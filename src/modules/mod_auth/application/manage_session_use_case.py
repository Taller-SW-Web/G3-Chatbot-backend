"""Customer session operations of SPEC-03: login, MFA, refresh, logout, profile (ADR-0018).

Thin orchestration over the Seguridad port (`SecurityPort`) and the local
access-token validator (`AccessTokenValidatorPort`): this use case never
stores tokens itself. Cookies are set by the HTTP edge; the refresh token
only travels in the `chat_rt` httpOnly cookie. The anonymous cart fusion is
resolved through the optional `CART_MERGE` capability so this module never
imports `mod-cart` (ADR-0024); its ordering invariant (merge before linking
conversations, which clears `anonymous_sid`) is enforced here.
"""
import logging
import time
import uuid

from src.shared.application.ports.cart_merge_port import CartMergePort, CartMergeResult
from src.shared.application.ports.checkout_document_port import CheckoutDocumentPort
from src.shared.application.ports.conversation_repository_port import ConversationRepositoryPort
from src.shared.application.ports.phone_verification_port import PhoneVerificationPort
from src.shared.application.ports.security_port import SecurityPort
from src.shared.application.ports.token_validator_port import (
    AccessTokenValidatorPort,
    AccountNotAllowedError,
    InvalidTokenError,
)
from src.shared.application.unit_of_work_port import UnitOfWorkPort
from src.shared.domain.masking import mask_email
from src.shared.domain.module_errors import ModuleRequestError

logger = logging.getLogger(__name__)

PASSWORD_POLICY_TTL_SECONDS = 10 * 60


def mask_document(document: dict) -> dict:
    """Masks the document number, keeping only its last three characters (SPEC-03 Req. 7)."""
    numero = str(document.get("numero", ""))
    masked = "*" * max(len(numero) - 3, 0) + numero[-3:] if numero else ""
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
        conversations: ConversationRepositoryPort | None = None,
        cart_merge: CartMergePort | None = None,
        uow: UnitOfWorkPort | None = None,
        clock=time.monotonic,
    ):
        self._security = security
        self._validator = validator
        self._phone_verifications = phone_verifications
        self._checkouts = checkouts
        self._conversations = conversations
        self._cart_merge = cart_merge
        self._uow = uow
        self._clock = clock
        self._password_policy: dict | None = None
        self._password_policy_at = 0.0

    async def register(
        self,
        *,
        email: str,
        password: str,
        phone: str,
        first_name: str,
        last_name: str,
    ) -> dict:
        """Creates the account in Seguridad and never opens a session (SPEC-01 Req. 3).

        The password is forwarded only; it is never logged, stored or sent to
        the LLM. The masked email is what the chat shows back.
        """
        answer = await self._security.register(
            email,
            password,
            phone,
            first_name=first_name,
            last_name=last_name,
        )
        return {
            "estado": str(answer.get("estado", "PENDIENTE_VERIFICACION")),
            "correoEnmascarado": mask_email(email),
        }

    async def password_policy(self) -> dict:
        """Password rules cached for 10 minutes (SPEC-01 Req. 2 / design)."""
        now = self._clock()
        if (
            self._password_policy is not None
            and now - self._password_policy_at < PASSWORD_POLICY_TTL_SECONDS
        ):
            return self._password_policy
        policy = dict(await self._security.password_policy())
        self._password_policy = policy
        self._password_policy_at = now
        return policy

    async def verify_email(self, token: str) -> dict:
        """Exchanges the single-use link; 410 codes come from Seguridad (SPEC-02 Req. 1)."""
        return await self._security.verify_email(token)

    async def resend_verification(self, email: str) -> dict:
        """Neutral re-send: the answer never reveals whether the account exists (SPEC-02 Req. 2)."""
        return await self._security.resend_verification(email)

    async def login(
        self,
        email: str,
        password: str,
        *,
        canal: str | None = None,
        anonymous_sid: str | None = None,
    ) -> dict:
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
        return await self._validated_session(answer, anonymous_sid=anonymous_sid)

    async def request_mfa_code(self, challenge_token: str, canal: str | None = None) -> dict:
        return await self._security.request_mfa(challenge_token, canal)

    async def verify_mfa_code(
        self,
        challenge_token: str,
        code: str,
        *,
        anonymous_sid: str | None = None,
    ) -> dict:
        try:
            answer = await self._security.verify_mfa(challenge_token, code)
        except ModuleRequestError as exc:
            if exc.code == "OTP_INTENTOS_AGOTADOS":
                # Not an "invalid code": Seguridad owns the status, the edge mirrors it.
                raise
            if exc.status == 401:
                raise InvalidCredentialsError(exc.intentos_restantes) from None
            raise
        return await self._validated_session(answer, anonymous_sid=anonymous_sid)

    async def _validated_session(
        self, answer: dict, *, anonymous_sid: str | None = None
    ) -> dict:
        """Validates the emitted access token before opening the session (SPEC-03 Req. 1).

        A token without the `CLIENTE` role (or an invalid token) discards the
        session in Seguridad and aborts, so no cookie/token reaches the client.
        When `anonymous_sid` is present, the anonymous cart is merged into the
        customer cart **before** anonymous conversations are linked (linking
        clears `anonymous_sid`, the cart lookup key) and any stored
        `accionPendiente` is consumed once. Both writes share one unit of work.
        """
        token = answer.get("accessToken")
        try:
            if not token:
                raise InvalidTokenError("Seguridad returned no access token.")
            claims = await self._validator.validate_customer(token)
        except (AccountNotAllowedError, InvalidTokenError):
            await self.logout(answer.get("refreshToken"))
            raise
        result = dict(answer)
        customer_id = self._customer_id(claims, result)
        try:
            merge = await self._merge_anonymous_cart(anonymous_sid, customer_id)
            if merge is not None:
                result["carritoFusionado"] = merge.merged
                result["avisoCarrito"] = merge.message
                result["ajustesCarrito"] = [dict(row) for row in merge.adjustments]
            pending_action = await self._link_anonymous_conversations(
                anonymous_sid=anonymous_sid,
                customer_id=customer_id,
            )
            if pending_action is not None:
                result["accionPendiente"] = pending_action
            if self._uow is not None and anonymous_sid and customer_id is not None:
                await self._uow.commit()
        except Exception:
            if self._uow is not None:
                await self._uow.rollback()
            raise
        return result

    @staticmethod
    def _customer_id(claims: object, answer: dict) -> uuid.UUID | None:
        """`sub` of the validated token, with Seguridad's `usuario.id` as fallback."""
        raw_sub = claims.get("sub") if isinstance(claims, dict) else None
        if not raw_sub:
            usuario = answer.get("usuario")
            raw_sub = usuario.get("id") if isinstance(usuario, dict) else None
        if not raw_sub:
            return None
        try:
            return uuid.UUID(str(raw_sub))
        except ValueError:
            return None

    async def _merge_anonymous_cart(
        self, anonymous_sid: str | None, customer_id: uuid.UUID | None
    ) -> CartMergeResult | None:
        """Merges the anonymous cart before `anonymous_sid` is cleared (SPEC-03 Req. 4).

        `mod-cart` owns the limits, duplicate-SKU sums and stock clamping; this
        use case only resolves the `CART_MERGE` capability and maps its result
        into the session answer. No commit here: `_validated_session` owns the
        transaction, so a failure leaves the anonymous cart intact for retry.
        """
        if not anonymous_sid or customer_id is None or self._cart_merge is None:
            return None
        return await self._cart_merge.merge_for_login(anonymous_sid, customer_id)

    async def _link_anonymous_conversations(
        self,
        *,
        anonymous_sid: str | None,
        customer_id: uuid.UUID | None,
    ) -> str | None:
        if not anonymous_sid or customer_id is None or self._conversations is None:
            return None
        _, pending_action = await self._conversations.link_anonymous_to_customer(
            anonymous_sid, customer_id
        )
        return pending_action

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
        document = (
            await self._checkouts.latest_document(uuid.UUID(str(customer_id)))
            if self._checkouts is not None
            else None
        )
        profile["ultimoDocumento"] = mask_document(document) if document else None
        return profile
