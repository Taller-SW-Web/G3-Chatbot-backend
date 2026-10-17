"""Customer session operations of SPEC-03: login, MFA, refresh, logout, profile (ADR-0018).

Thin orchestration over the Seguridad port (`SecurityPort`) and the local
access-token validator (`AccessTokenValidatorPort`): this use case never
stores tokens itself. Cookies are set by the HTTP edge; the refresh token
only travels in the `chat_rt` httpOnly cookie. Cart fusion is a pure function
so the cart flow can reuse it when linking the anonymous cart on login
(SPEC-03 Req. 4).
"""
import logging
import uuid

from src.ports.outbound.checkout_document_port import CheckoutDocumentPort
from src.ports.outbound.module_errors import ModuleRequestError
from src.ports.outbound.phone_verification_port import PhoneVerificationPort
from src.ports.outbound.security_port import SecurityPort
from src.ports.outbound.token_validator_port import (
    AccessTokenValidatorPort,
    AccountNotAllowedError,
    InvalidTokenError,
)

logger = logging.getLogger(__name__)

MAX_QTY_PER_LINE = 10


def merge_cart_lines(anonymous_lines: list[dict], customer_lines: list[dict]) -> list[dict]:
    """Merges the anonymous cart into the customer cart (SPEC-03 Req. 4).

    Same SKU sums quantities capped at 10; stock clamping happens against
    live availability in the cart flow (SPEC-10/11). The anonymous cart is
    left for the caller to mark MERGED.
    """
    merged: dict[str, dict] = {}
    for line in list(customer_lines) + list(anonymous_lines):
        sku = str(line.get("sku"))
        qty = int(line.get("cantidad", 0))
        current = merged.get(sku, {"sku": sku, "cantidad": 0})
        current["cantidad"] = min(MAX_QTY_PER_LINE, int(current["cantidad"]) + max(qty, 0))
        merged[sku] = current
    return list(merged.values())


def mask_document(document: dict) -> dict:
    """Masks the document number, keeping only its last four digits (modelo-datos.md §AccountPage)."""
    numero = str(document.get("numero", ""))
    masked = "*" * max(len(numero) - 4, 0) + numero[-4:] if numero else ""
    return {"tipo": document.get("tipo"), "numeroEnmascarado": masked}


class InvalidCredentialsError(Exception):
    """Wrong password, blocked account or wrong OTP. May carry the remaining OTP attempts."""

    def __init__(self, intentos_restantes: int | None = None):
        self.intentos_restantes = intentos_restantes
        super().__init__("invalid credentials")


class RefreshInvalidError(Exception):
    pass


class ManageSessionUseCase:
    def __init__(
        self,
        security: SecurityPort,
        validator: AccessTokenValidatorPort,
        *,
        phone_verifications: PhoneVerificationPort | None = None,
        checkouts: CheckoutDocumentPort | None = None,
    ):
        self._security = security
        self._validator = validator
        self._phone_verifications = phone_verifications
        self._checkouts = checkouts

    async def login(self, email: str, password: str, *, canal: str | None = None) -> dict:
        """Returns the Seguridad answer: opened tokens or `{mfaRequerido, challengeToken, canal}`.

        When MFA is required, the OTP is requested here (SPEC-03 Req. 2); the
        `/mfa/solicitar` endpoint stays for resend / channel switch.
        """
        try:
            answer = await self._security.login(email, password)
        except ModuleRequestError as exc:
            if exc.status == 401:
                raise InvalidCredentialsError() from None
            raise
        if answer.get("mfaRequerido"):
            challenge = answer.get("challengeToken")
            if challenge:
                await self._security.request_mfa(challenge, canal or answer.get("canal"))
            return answer
        return await self._validated_session(answer)

    async def request_mfa_code(self, challenge_token: str, canal: str | None = None) -> dict:
        return await self._security.request_mfa(challenge_token, canal)

    async def verify_mfa_code(self, challenge_token: str, code: str) -> dict:
        try:
            answer = await self._security.verify_mfa(challenge_token, code)
        except ModuleRequestError as exc:
            if exc.code == "OTP_INTENTOS_AGOTADOS":
                # Not an "invalid code": Seguridad owns the status, the edge mirrors it.
                raise
            if exc.status == 401:
                raise InvalidCredentialsError(exc.intentos_restantes) from None
            raise
        return await self._validated_session(answer)

    async def _validated_session(self, answer: dict) -> dict:
        """Validates the emitted access token before opening the session (SPEC-03 Req. 1).

        A token without the `CLIENTE` role (or an invalid token) discards the
        session in Seguridad and aborts, so no cookie/token reaches the client.
        """
        token = answer.get("accessToken")
        try:
            if not token:
                raise InvalidTokenError("Seguridad returned no access token.")
            await self._validator.validate_customer(token)
        except (AccountNotAllowedError, InvalidTokenError):
            await self.logout(answer.get("refreshToken"))
            raise
        return answer

    async def refresh(self, refresh_token: str) -> dict:
        try:
            return await self._security.refresh(refresh_token)
        except ModuleRequestError as exc:
            if exc.code == "REFRESCO_INVALIDO" or exc.status == 401:
                raise RefreshInvalidError() from None
            raise

    async def logout(self, refresh_token: str | None) -> None:
        """Best effort: the local session always closes even if Seguridad is down (SPEC-03 Req. 6)."""
        if not refresh_token:
            return
        try:
            await self._security.logout(refresh_token)
        except Exception:
            logger.warning("Seguridad logout failed; clearing the local session anyway")

    async def profile(self, holder_token: str, customer_id: str) -> dict:
        """`GET /auth/me` plus `celularVerificado` and the masked `ultimoDocumento`."""
        profile = dict(await self._security.me(holder_token))
        phone = profile.get("celular") or profile.get("telefono")
        verified = False
        if phone and self._phone_verifications is not None:
            verified = await self._phone_verifications.existe(uuid.UUID(str(customer_id)), phone)
        profile["celularVerificado"] = verified
        document = None
        if self._checkouts is not None:
            document = await self._checkouts.latest_document(uuid.UUID(str(customer_id)))
        profile["ultimoDocumento"] = mask_document(document) if document else None
        return profile
