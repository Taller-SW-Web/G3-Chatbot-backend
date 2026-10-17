"""Customer session endpoints (SPEC-03, contratos-integracion.md 2.2). Thin: no business rules.

Login/MFA open the session, refresh rotates it, logout closes it. The
refresh token and the MFA challenge token only travel in httpOnly cookies
(`chat_rt` rotates, `chat_chg` 5 min); the access token goes to the frontend
body and lives in LocalStorage there. Mutating session calls require the
`X-Requested-With` header plus an exact `Origin` match against the configured
allowlist (ADR-0023).
"""
from collections.abc import Callable, Sequence
import logging
from urllib.parse import urlsplit

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
from src.ports.outbound.token_validator_port import AccountNotAllowedError, InvalidTokenError

logger = logging.getLogger(__name__)

REFRESH_COOKIE = "chat_rt"
CHALLENGE_COOKIE = "chat_chg"
SESSION_COOKIE_PATH = "/api/v1/sesion"
CHALLENGE_MAX_AGE_SECONDS = 5 * 60
REFRESH_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def _normalize_origin(value: str) -> str:
    """`https://host:port` scheme+authority, lowercased; empty when not a valid origin."""
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def _csrf_check(request: Request, allowed_origins: Sequence[str]) -> None:
    if not request.headers.get("x-requested-with"):
        raise ProblemError(403, "PETICION_INVALIDA", "Invalid request",
                           "Missing X-Requested-With header.")
    origin = request.headers.get("origin")
    if origin and _normalize_origin(origin) not in set(allowed_origins):
        raise ProblemError(403, "PETICION_INVALIDA", "Invalid request", "Origin mismatch.")


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path=SESSION_COOKIE_PATH)
    response.delete_cookie(CHALLENGE_COOKIE, path=SESSION_COOKIE_PATH)


def _account_not_allowed() -> ProblemError:
    return ProblemError(
        403,
        "CUENTA_NO_PERMITIDA",
        "Account not allowed",
        "Esta cuenta no puede comprar desde este canal",
    )


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)
    canal: str = Field(default="EMAIL", pattern=r"^(EMAIL|SMS)$")


class VerifyMfaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class RequestMfaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canal: str = Field(default="EMAIL", pattern=r"^(EMAIL|SMS)$")


class OpenedSession(BaseModel):
    accessToken: str
    expiresIn: int = 900
    usuario: dict = Field(default_factory=dict)


class MfaChallenge(BaseModel):
    mfaRequerido: bool = True
    canal: str = "EMAIL"


class OkResponse(BaseModel):
    ok: bool = True


def create_session_router(
    session_provider: Callable[..., ManageSessionUseCase],
    *,
    cookie_secure: bool,
    allowed_origins: Sequence[str] = (),
) -> APIRouter:
    """`session_provider` is a FastAPI dependency; infrastructure wires it, tests replace it."""
    router = APIRouter(prefix="/sesion", tags=["sesion"])

    def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
        response.set_cookie(
            REFRESH_COOKIE, refresh_token, httponly=True, secure=cookie_secure,
            samesite="strict", path=SESSION_COOKIE_PATH, max_age=REFRESH_MAX_AGE_SECONDS,
        )

    def _open_session_response(response: Response, answer: dict) -> OpenedSession:
        refresh_token = answer.get("refreshToken")
        if refresh_token:
            _set_refresh_cookie(response, str(refresh_token))
        return OpenedSession(
            accessToken=str(answer.get("accessToken")),
            expiresIn=int(answer.get("expiresIn", 900)),
            usuario=dict(answer.get("usuario", {})),
        )

    @router.post("/login", response_model=OpenedSession | MfaChallenge)
    async def login(
        body: LoginIn,
        request: Request,
        response: Response,
        sessions: ManageSessionUseCase = Depends(session_provider),
    ):
        _csrf_check(request, allowed_origins)
        try:
            answer = await sessions.login(body.email, body.password, canal=body.canal)
        except InvalidCredentialsError:
            raise ProblemError(401, "CREDENCIALES_INVALIDAS", "Invalid credentials",
                               "Correo o contraseña incorrectos") from None
        except AccountNotAllowedError:
            raise _account_not_allowed() from None
        except InvalidTokenError:
            raise ProblemError(401, "TOKEN_INVALIDO", "Invalid token",
                               "Seguridad devolvió un token inválido") from None
        except ModuleRequestError as exc:
            raise ProblemError(exc.status, exc.code, "Module request failed") from None
        if answer.get("mfaRequerido"):
            challenge = str(answer.get("challengeToken") or "")
            if challenge:
                response.set_cookie(
                    CHALLENGE_COOKIE, challenge, httponly=True, secure=cookie_secure,
                    samesite="strict", path=SESSION_COOKIE_PATH, max_age=CHALLENGE_MAX_AGE_SECONDS,
                )
            return MfaChallenge(canal=str(answer.get("canal", "EMAIL")))
        return _open_session_response(response, answer)

    @router.post("/mfa/solicitar", response_model=OkResponse)
    async def request_mfa(
        body: RequestMfaIn,
        request: Request,
        sessions: ManageSessionUseCase = Depends(session_provider),
    ):
        _csrf_check(request, allowed_origins)
        challenge = request.cookies.get(CHALLENGE_COOKIE)
        if not challenge:
            raise ProblemError(410, "CODIGO_VENCIDO", "Code expired", "Pide un código nuevo")
        try:
            await sessions.request_mfa_code(challenge, body.canal)
        except ModuleRequestError as exc:
            if exc.status == 429:
                raise ProblemError(429, "LIMITE_REENVIOS", "Too many requests",
                                   "Espera unos minutos antes de pedir otro código") from None
            raise ProblemError(exc.status, exc.code, "Module request failed") from None
        return OkResponse()

    @router.post("/mfa/verificar", response_model=OpenedSession)
    async def verify_mfa(
        body: VerifyMfaIn,
        request: Request,
        response: Response,
        sessions: ManageSessionUseCase = Depends(session_provider),
    ):
        _csrf_check(request, allowed_origins)
        challenge = request.cookies.get(CHALLENGE_COOKIE)
        if not challenge:
            raise ProblemError(410, "CODIGO_VENCIDO", "Code expired", "Pide un código nuevo")
        try:
            answer = await sessions.verify_mfa_code(challenge, body.code)
        except InvalidCredentialsError as exc:
            extra = ({"intentosRestantes": exc.intentos_restantes}
                     if exc.intentos_restantes is not None else {})
            raise ProblemError(401, "CODIGO_INVALIDO", "Invalid code",
                               "Código incorrecto", **extra) from None
        except AccountNotAllowedError:
            # The injected `response` is discarded when a problem is raised, so the
            # cookie deletions travel on the problem itself.
            error = _account_not_allowed()
            cleared = Response()
            cleared.delete_cookie(CHALLENGE_COOKIE, path=SESSION_COOKIE_PATH)
            cleared.delete_cookie(REFRESH_COOKIE, path=SESSION_COOKIE_PATH)
            error.headers = [
                (name.decode("latin-1"), value.decode("latin-1"))
                for name, value in cleared.raw_headers
                if name == b"set-cookie"
            ]
            raise error from None
        except InvalidTokenError:
            raise ProblemError(401, "TOKEN_INVALIDO", "Invalid token",
                               "Seguridad devolvió un token inválido") from None
        except ModuleRequestError as exc:
            if exc.code == "OTP_INTENTOS_AGOTADOS":
                raise ProblemError(exc.status, exc.code, "No attempts left",
                                   "Pide un código nuevo") from None
            raise ProblemError(exc.status, exc.code, "Module request failed") from None
        response.delete_cookie(CHALLENGE_COOKIE, path=SESSION_COOKIE_PATH)
        return _open_session_response(response, answer)

    @router.post("/refresh", response_model=OpenedSession)
    async def refresh(
        request: Request,
        response: Response,
        sessions: ManageSessionUseCase = Depends(session_provider),
    ):
        _csrf_check(request, allowed_origins)
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

    @router.post("/logout", response_model=OkResponse)
    async def logout(
        request: Request,
        response: Response,
        sessions: ManageSessionUseCase = Depends(session_provider),
    ):
        _csrf_check(request, allowed_origins)
        try:
            await sessions.logout(request.cookies.get(REFRESH_COOKIE))
        except Exception:
            # Logout is best effort: the local session must close even if Seguridad is down (Req. 6).
            logger.warning("Seguridad logout failed; clearing the local session anyway")
        _clear_session_cookies(response)
        return OkResponse()

    @router.get("/perfil")
    async def profile(
        request: Request,
        sessions: ManageSessionUseCase = Depends(session_provider),
        caller: CallerIdentity = Depends(get_current_customer),
    ):
        authorization = request.headers.get("authorization", "")
        holder_token = authorization.partition(" ")[2].strip()
        try:
            return await sessions.profile(holder_token, str(caller.customer_id))
        except ModuleRequestError as exc:
            raise ProblemError(exc.status, exc.code, "Module request failed") from None

    return router
