"""Customer session endpoints (SPEC-01..03, contratos-integracion.md 2.2). Thin: no business rules.

Registration and email verification never open a session (SPEC-01/02);
login/MFA open it, refresh rotates it and logout closes it. The refresh token
and the MFA challenge token only travel in httpOnly cookies (`chat_rt`
rotates, `chat_chg` 5 min); the access token goes to the frontend body and
lives in LocalStorage there. Mutating session calls require the
`X-Requested-With` header plus an exact `Origin` match against the configured
allowlist (ADR-0023). Public registration endpoints are rate limited per IP
and per email (SPEC-01/02).
"""
from collections.abc import Callable, Sequence
import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.adapters.inbound.http.dependencies.jwt_validator import get_current_customer
from src.adapters.inbound.http.dependencies.rate_limiter import (
    REGISTER_DEBOUNCE_PER_EMAIL,
    REGISTER_PER_IP,
    RESEND_PER_EMAIL,
    InMemoryRateLimiter,
    client_ip,
)
from src.adapters.inbound.http.problem import ProblemError
from src.application.use_cases.manage_session_use_case import (
    InvalidCredentialsError,
    ManageSessionUseCase,
    RefreshInvalidError,
)
from src.domain.value_objects.caller_identity import CallerIdentity
from src.ports.outbound.module_errors import ModuleRequestError, ModuleUnavailableError
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


DEFAULT_POLICY_MESSAGE = "La contraseña no cumple la política de seguridad"

# Seguridad's register field names -> the BFF request names. Seguridad's
# payload is email/password/phone/nombres/apellidos (security_client.register);
# unknown names pass through unchanged.
SECURITY_FIELD_TO_BFF = {"nombres": "firstName", "apellidos": "lastName"}


def _scrub(text: str, password: str) -> str:
    """Never echo the submitted password back to the client."""
    if password and password in text:
        return text.replace(password, "***")
    return text


def _register_field_errors(errors: list[dict[str, str]], password: str) -> list[dict[str, str]]:
    return [
        {
            "campo": SECURITY_FIELD_TO_BFF.get(row["campo"], row["campo"]),
            "mensaje": _scrub(row["mensaje"], password),
        }
        for row in errors
    ]


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


class RegistroIn(BaseModel):
    """SPEC-01 Req. 1: the chatbot collects these fields before the BFF call."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=5, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=1, max_length=256)
    phone: str = Field(pattern=r"^\+?[0-9]{9,15}$")
    firstName: str = Field(min_length=1, max_length=80)
    lastName: str = Field(min_length=1, max_length=80)
    acceptTerms: bool

    @field_validator("acceptTerms")
    @classmethod
    def _accept_terms(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Debes aceptar los términos y condiciones")
        return value


class RegistroOut(BaseModel):
    """The chat turns this into its confirmation message; the phone is not echoed back."""

    estado: str
    correoEnmascarado: str


class VerificarCorreoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=8, max_length=512)


class ReenviarVerificacionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=5, max_length=254)


def create_session_router(
    session_provider: Callable[..., ManageSessionUseCase],
    *,
    cookie_secure: bool,
    allowed_origins: Sequence[str] = (),
    rate_limiter: InMemoryRateLimiter | None = None,
) -> APIRouter:
    """`session_provider` is a FastAPI dependency; infrastructure wires it, tests replace it."""
    router = APIRouter(prefix="/sesion", tags=["sesion"])
    limiter = rate_limiter or InMemoryRateLimiter()

    def _service_unavailable() -> ProblemError:
        return ProblemError(
            503,
            "SERVICIO_NO_DISPONIBLE",
            "Service unavailable",
            "No pudimos completar la operación en este momento. Intenta de nuevo en unos minutos.",
        )

    def _too_many(code: str, detail: str, retry_after: int) -> ProblemError:
        return ProblemError(
            429,
            code,
            "Too many requests",
            detail,
            headers=[("Retry-After", str(retry_after))],
            retryAfter=retry_after,
        )

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

    @router.post("/registro", response_model=RegistroOut, status_code=201)
    async def register(
        body: RegistroIn,
        request: Request,
        sessions: ManageSessionUseCase = Depends(session_provider),
    ):
        """Creates a `PENDIENTE_VERIFICACION` account; it never opens a session (SPEC-01)."""
        _csrf_check(request, allowed_origins)
        blocked = await limiter.hit_any(
            [
                (REGISTER_DEBOUNCE_PER_EMAIL, body.email.strip().lower()),
                (REGISTER_PER_IP, client_ip(request)),
            ]
        )
        if blocked is not None:
            rule, retry_after = blocked
            if rule is REGISTER_DEBOUNCE_PER_EMAIL:
                raise _too_many(
                    "SOLICITUD_EN_CURSO",
                    "Ya estamos procesando tu registro, espera unos segundos.",
                    retry_after,
                )
            raise _too_many(
                "DEMASIADAS_SOLICITUDES",
                "Se crearon demasiadas cuentas desde esta red. Intenta más tarde.",
                retry_after,
            )
        try:
            return await sessions.register(
                email=body.email.strip(),
                password=body.password,
                phone=body.phone,
                first_name=body.firstName,
                last_name=body.lastName,
            )
        except ModuleRequestError as exc:
            if exc.status == 409:
                # Neutral: it does not confirm that the email is already registered
                # beyond Seguridad's own availability answer (SPEC-01 Req. 4).
                raise ProblemError(409, "CORREO_NO_DISPONIBLE", "Email not available",
                                   "Ese correo no está disponible para registro") from None
            if exc.status == 400 and exc.code == "VALIDACION" and exc.errors:
                raise ProblemError(
                    400, "VALIDACION", "Validation failed",
                    errores=_register_field_errors(exc.errors, body.password),
                ) from None
            if exc.status == 422 and exc.code == "POLITICA_INCUMPLIDA":
                detail = _scrub(exc.detail, body.password) or DEFAULT_POLICY_MESSAGE
                raise ProblemError(
                    422, "POLITICA_INCUMPLIDA", "Password policy not met", detail,
                    errores=[{"campo": "password", "mensaje": detail}],
                ) from None
            raise ProblemError(exc.status, exc.code, "Module request failed") from None
        except ModuleUnavailableError:
            raise _service_unavailable() from None

    @router.get("/politica-contrasena")
    async def password_policy(sessions: ManageSessionUseCase = Depends(session_provider)):
        """Password rules the chat renders before asking for one (SPEC-01 Req. 2)."""
        try:
            return await sessions.password_policy()
        except ModuleRequestError as exc:
            raise ProblemError(exc.status, exc.code, "Module request failed") from None
        except ModuleUnavailableError:
            raise _service_unavailable() from None

    @router.post("/verificar-correo", status_code=204)
    async def verify_email(
        body: VerificarCorreoIn,
        request: Request,
        sessions: ManageSessionUseCase = Depends(session_provider),
    ) -> Response:
        """Consumes the single-use link; expired/used links answer 410 with a code (SPEC-02)."""
        _csrf_check(request, allowed_origins)
        try:
            await sessions.verify_email(body.token)
        except ModuleRequestError as exc:
            raise ProblemError(exc.status, exc.code, "Email verification failed") from None
        except ModuleUnavailableError:
            raise _service_unavailable() from None
        return Response(status_code=204)

    @router.post("/verificar-correo/reenviar", response_model=OkResponse, status_code=202)
    async def resend_verification(
        body: ReenviarVerificacionIn,
        request: Request,
        sessions: ManageSessionUseCase = Depends(session_provider),
    ):
        """Always neutral: the same 202 whether or not the account exists (SPEC-02 Req. 2)."""
        _csrf_check(request, allowed_origins)
        retry_after = await limiter.hit(RESEND_PER_EMAIL, body.email.strip().lower())
        if retry_after is not None:
            raise _too_many(
                "DEMASIADAS_SOLICITUDES",
                "Ya enviamos varios enlaces a ese correo. Revisa tu bandeja o intenta más tarde.",
                retry_after,
            )
        try:
            await sessions.resend_verification(body.email.strip())
        except ModuleRequestError as exc:
            if exc.status == 429:
                raise _too_many(
                    "DEMASIADAS_SOLICITUDES",
                    "Ya enviamos varios enlaces a ese correo. Intenta más tarde.",
                    60,
                ) from None
            raise ProblemError(exc.status, exc.code, "Module request failed") from None
        except ModuleUnavailableError:
            raise _service_unavailable() from None
        return OkResponse()

    return router
