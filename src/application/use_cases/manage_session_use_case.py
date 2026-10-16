"""Customer session operations of SPEC-03: login, MFA, refresh, logout, profile (ADR-0018).

Thin orchestration over the Seguridad client: this use case never stores
tokens itself. Cookies are set by the HTTP edge; the refresh token only
travels in the `chat_rt` httpOnly cookie. Cart fusion is a pure function
so cart owners can reuse it when linking the anonymous cart on login
(SPEC-03 Req. 4).
"""
from dataclasses import dataclass

from src.adapters.outbound.http_clients.security_client import SecurityClient
from src.ports.outbound.module_errors import ModuleRequestError

MAX_QTY_PER_LINE = 10


@dataclass(frozen=True)
class SessionTokens:
    access_token: str
    expires_in: int
    user: dict


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


class InvalidCredentialsError(Exception):
    pass


class RefreshInvalidError(Exception):
    pass


class ManageSessionUseCase:
    def __init__(self, security: SecurityClient):
        self._security = security

    async def login(self, email: str, password: str) -> dict:
        """Returns the Seguridad answer: tokens or `{mfaRequerido, challengeToken, canal}`."""
        try:
            return await self._security.login(email, password)
        except ModuleRequestError as exc:
            if exc.status == 401:
                raise InvalidCredentialsError() from None
            raise

    async def request_mfa_code(self, challenge_token: str) -> dict:
        return await self._security.request_mfa(challenge_token)

    async def verify_mfa_code(self, challenge_token: str, code: str) -> dict:
        try:
            return await self._security.verify_mfa(challenge_token, code)
        except ModuleRequestError as exc:
            if exc.status == 401:
                raise InvalidCredentialsError() from None
            raise

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
            return

    async def profile(self, holder_token: str) -> dict:
        return await self._security.me(holder_token)

    def build_opened_session(self, answer: dict) -> SessionTokens:
        return SessionTokens(
            access_token=str(answer.get("accessToken", "")),
            expires_in=int(answer.get("expiresIn", 900)),
            user=dict(answer.get("usuario", {})),
        )
