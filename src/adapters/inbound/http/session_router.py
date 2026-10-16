"""Customer session endpoints (SPEC-03, contratos-integracion.md 2.2). Thin: no business rules.

Login/MFA open the session, refresh rotates it, logout closes it. The
refresh token and the MFA challenge token only travel in httpOnly cookies
(`chat_rt` 15 min rotation, `chat_chg` 5 min); the access token goes to the
frontend body and lives in LocalStorage there. Mutating session calls
require the `X-Requested-With` header plus `Origin` verification.
"""
from collections.abc import Callable

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from src.adapters.inbound.http.dependencies.jwt_validator import get_current_customer
from src.adapters.inbound.http.problem import ProblemError
from src.application.use_cases.manage_session_use_case import (
    InvalidCredentialsError,
    ManageSessionUseCase,
    RefreshInvalidError,
)
from src.domain.value_objects.caller_identity import CallerIdentity
from src.ports.outbound.module_errors import ModuleRequestError

REFRESH_COOKIE = "chat_rt"
CHALLENGE_COOKIE = "chat_chg"
SESSION_COOKIE_PATH = "/api/v1/sesion"
CHALLENGE_MAX_AGE_SECONDS = 5 * 60


def _csrf_check(request: Request) -> None:
    if not request.headers.get("x-requested-with"):
        raise ProblemError(403, "PETICION_INVALIDA", "Invalid request",
                           "Missing X-Requested-With header.")
    origin = request.headers.get("origin")
    if origin:
        host = request.headers.get("host", "")
        if host and host not in origin:
            raise ProblemError(403, "PETICION_INVALIDA", "Invalid request",
                               "Origin mismatch.")


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path=SESSION_COOKIE_PATH)
    response.delete_cookie(CHALLENGE_COOKIE, path=SESSION_COOKIE_PATH)


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class VerifyMfaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class RequestMfaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canal: str = Field(default="EMAIL", pattern=r"^(EMAIL|SMS)$")


def create_session_router(
    session_provider: Callable[..., ManageSessionUseCase],
    *,
    cookie_secure: bool,
) -> APIRouter:
    """`session_provider` is a FastAPI dependency; infrastructure wires it, tests replace it."""
    router = APIRouter(prefix="/sesion", tags=["sesion"])

    def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
        response.set_cookie(
            REFRESH_COOKIE, refresh_token, httponly=True, secure=cookie_secure,
            samesite="strict", path=SESSION_COOKIE_PATH,
        )

    def _open_session_response(response: Response, answer: dict) -> dict:
        refresh_token = answer.get("refreshToken")
        if refresh_token:
            _set_refresh_cookie(response, str(refresh_token))
        opened = {"accessToken": answer.get("accessToken"),
                  "expiresIn": answer.get("expiresIn", 900),
                  "usuario": answer.get("usuario", {})}
        return opened

    @router.post("/login")
    async def login(
        body: LoginIn,
        request: Request,
        response: Response,
        sessions: ManageSessionUseCase = Depends(session_provider),
    ):
        _csrf_check(request)
        try:
            answer = await sessions.login(body.email, body.password)
        except InvalidCredentialsError:
            raise ProblemError(401, "CREDENCIALES_INVALIDAS", "Invalid credentials",
                               "Correo o contraseña incorrectos") from None
        if answer.get("mfaRequerido"):
            response.set_cookie(
                CHALLENGE_COOKIE, str(answer.get("challengeToken", "")), httponly=True,
                secure=cookie_secure, samesite="strict", path=SESSION_COOKIE_PATH,
                max_age=CHALLENGE_MAX_AGE_SECONDS,
            )
            return {"mfaRequerido": True, "canal": answer.get("canal", "EMAIL")}
        return _open_session_response(response, answer)

    @router.post("/mfa/solicitar")
    async def request_mfa(
        body: RequestMfaIn,
        request: Request,
        sessions: ManageSessionUseCase = Depends(session_provider),
    ):
        _csrf_check(request)
        challenge = request.cookies.get(CHALLENGE_COOKIE)
        if not challenge:
            raise ProblemError(410, "CODIGO_VENCIDO", "Code expired", "Pide un código nuevo")
        try:
            await sessions.request_mfa_code(challenge)
        except ModuleRequestError as exc:
            if exc.status == 429:
                raise ProblemError(429, "LIMITE_REENVIOS", "Too many requests",
                                   "Espera unos minutos antes de pedir otro código") from None
            raise ProblemError(exc.status, exc.code, "Module request failed") from None
        return {"ok": True}

    @router.post("/mfa/verificar")
    async def verify_mfa(
        body: VerifyMfaIn,
        request: Request,
        response: Response,
        sessions: ManageSessionUseCase = Depends(session_provider),
    ):
        _csrf_check(request)
        challenge = request.cookies.get(CHALLENGE_COOKIE)
        if not challenge:
            raise ProblemError(410, "CODIGO_VENCIDO", "Code expired", "Pide un código nuevo")
        try:
            answer = await sessions.verify_mfa_code(challenge, body.code)
        except InvalidCredentialsError:
            raise ProblemError(401, "CODIGO_INVALIDO", "Invalid code",
                               "Código incorrecto") from None
        except ModuleRequestError as exc:
            if exc.code in ("OTP_INTENTOS_AGOTADOS",):
                raise ProblemError(403, exc.code, "No attempts left",
                                   "Pide un código nuevo") from None
            raise ProblemError(exc.status, exc.code, "Module request failed") from None
        response.delete_cookie(CHALLENGE_COOKIE, path=SESSION_COOKIE_PATH)
        return _open_session_response(response, answer)

    @router.post("/refresh")
    async def refresh(
        request: Request,
        response: Response,
        sessions: ManageSessionUseCase = Depends(session_provider),
    ):
        _csrf_check(request)
        refresh_token = request.cookies.get(REFRESH_COOKIE)
        if not refresh_token:
            raise ProblemError(401, "REFRESCO_INVALIDO", "Invalid refresh",
                               "Tu sesión terminó, vuelve a iniciar sesión")
        try:
            answer = await sessions.refresh(refresh_token)
        except RefreshInvalidError:
            _clear_session_cookies(response)
            raise ProblemError(401, "REFRESCO_INVALIDO", "Invalid refresh",
                               "Tu sesión terminó, vuelve a iniciar sesión") from None
        return _open_session_response(response, answer)

    @router.post("/logout")
    async def logout(
        request: Request,
        response: Response,
        sessions: ManageSessionUseCase = Depends(session_provider),
    ):
        _csrf_check(request)
        await sessions.logout(request.cookies.get(REFRESH_COOKIE))
        _clear_session_cookies(response)
        return {"ok": True}

    @router.get("/perfil")
    async def profile(
        request: Request,
        sessions: ManageSessionUseCase = Depends(session_provider),
        caller: CallerIdentity = Depends(get_current_customer),
    ):
        authorization = request.headers.get("authorization", "")
        holder_token = authorization.partition(" ")[2].strip()
        try:
            return await sessions.profile(holder_token)
        except ModuleRequestError as exc:
            raise ProblemError(exc.status, exc.code, "Module request failed") from None

    return router
