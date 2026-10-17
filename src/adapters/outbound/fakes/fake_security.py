"""In-process Seguridad adapters (SPEC-01..03, ADR-0022).

Selected with `MODULES_MODE=fake` for deployed environments without the real
module. They implement the same outbound ports as the HTTP clients and keep
their state in memory, seeded from `seeds/security.json`, so the demo flow
(registration -> email verification -> login) works end to end without
network. Never enables itself: the composition root reads `MODULES_MODE`.
"""
import json
import time
import uuid
from pathlib import Path
from typing import Any

from src.ports.outbound.module_errors import ModuleRequestError
from src.ports.outbound.token_validator_port import InvalidTokenError

SEEDS = Path(__file__).resolve().parent / "seeds"
VERIFICATION_TTL_SECONDS = 24 * 60 * 60
ACCESS_TOKEN_PREFIX = "fake-access-"
REFRESH_TOKEN_PREFIX = "fake-refresh-"
SERVICE_TOKEN = "fake-service-token"


def load_seed(name: str):
    with open(SEEDS / name, encoding="utf-8") as handle:
        return json.load(handle)


def _fold(email: str) -> str:
    return email.strip().lower()


class FakeSecurityAdapter:
    """In-memory Seguridad: one shared instance keeps registrations between requests."""

    module = "security"

    def __init__(
        self,
        policy: dict | None = None,
        customers: list[dict] | None = None,
        *,
        clock=time.monotonic,
    ):
        seed = load_seed("security.json")
        self._policy = dict(policy if policy is not None else seed["policy"])
        source = customers if customers is not None else seed["customers"]
        self._customers = {_fold(item["email"]): dict(item) for item in source}
        self._verifications: dict[str, dict] = {}
        self._clock = clock

    async def register(
        self,
        email: str,
        password: str,
        phone: str,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> dict[str, Any]:
        key = _fold(email)
        if key in self._customers:
            raise ModuleRequestError(self.module, 409, "CORREO_NO_DISPONIBLE")
        customer_id = str(uuid.uuid4())
        full_name = " ".join(part for part in (first_name, last_name) if part) or None
        self._customers[key] = {
            "id": customer_id,
            "email": key,
            "password": password,
            "phone": phone,
            "estado": "PENDIENTE_VERIFICACION",
            "nombreCompleto": full_name,
        }
        self._issue_verification(key)
        return {"customerId": customer_id, "estado": "PENDIENTE_VERIFICACION"}

    async def password_policy(self) -> dict[str, Any]:
        return dict(self._policy)

    async def verify_email(self, token: str) -> dict[str, Any]:
        entry = self._verifications.get(token)
        if entry is None or entry["expires_at"] <= self._clock():
            raise ModuleRequestError(self.module, 410, "ENLACE_EXPIRADO")
        if entry["used"]:
            raise ModuleRequestError(self.module, 410, "ENLACE_YA_USADO")
        entry["used"] = True
        customer = self._customers.get(entry["email"])
        if customer is not None:
            customer["estado"] = "ACTIVO"
        return {}

    async def resend_verification(self, email: str) -> dict[str, Any]:
        # Neutral on purpose: an unknown email is answered exactly like a known one.
        key = _fold(email)
        if key in self._customers:
            self._issue_verification(key)
        return {}

    async def login(self, email: str, password: str) -> dict[str, Any]:
        customer = self._customers.get(_fold(email))
        # Like the real module: wrong password, unverified, blocked or inactive
        # accounts all answer the same 401 (the cause is never revealed).
        if (
            customer is None
            or customer.get("password") != password
            or customer.get("estado") != "ACTIVO"
        ):
            raise ModuleRequestError(self.module, 401, "CREDENCIALES_INVALIDAS")
        return self._open_session(customer)

    async def request_mfa(self, challenge_token: str, canal: str | None = None) -> dict[str, Any]:
        return {"canal": canal or "EMAIL"}

    async def verify_mfa(self, challenge_token: str, code: str) -> dict[str, Any]:
        customer = next(iter(self._customers.values()), None)
        if customer is None:
            raise ModuleRequestError(self.module, 401, "CODIGO_INVALIDO")
        return self._open_session(customer)

    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        customer = self._customer_by_id(refresh_token.removeprefix(REFRESH_TOKEN_PREFIX))
        if customer is None:
            raise ModuleRequestError(self.module, 401, "REFRESCO_INVALIDO")
        return self._open_session(customer)

    async def logout(self, refresh_token: str | None) -> dict[str, Any]:
        return {}

    async def me(self, holder_token: str) -> dict[str, Any]:
        customer = self._customer_by_id(holder_token.removeprefix(ACCESS_TOKEN_PREFIX))
        if customer is None:
            raise ModuleRequestError(self.module, 401, "TOKEN_INVALIDO")
        return {
            "id": customer["id"],
            "correo": customer["email"],
            "nombreCompleto": customer.get("nombreCompleto"),
            "celular": customer.get("phone"),
        }

    def claims_for_token(self, token: str) -> dict[str, Any] | None:
        """Used by `FakeAccessTokenValidator`; `None` when the token is unknown."""
        customer = self._customer_by_id(token.removeprefix(ACCESS_TOKEN_PREFIX))
        if customer is None:
            return None
        return {"sub": customer["id"], "rol": "CLIENTE", "email": customer["email"]}

    def verification_token(self, email: str) -> str | None:
        """Demo/test helper: the last link token for `email` (no email is actually sent)."""
        key = _fold(email)
        return next(
            (
                token
                for token, entry in reversed(list(self._verifications.items()))
                if entry["email"] == key and not entry["used"]
            ),
            None,
        )

    def _issue_verification(self, email: str) -> str:
        token = f"fake-verificacion-{uuid.uuid4().hex}"
        self._verifications[token] = {
            "email": email,
            "expires_at": self._clock() + VERIFICATION_TTL_SECONDS,
            "used": False,
        }
        return token

    def _customer_by_id(self, customer_id: str) -> dict | None:
        return next((item for item in self._customers.values() if item["id"] == customer_id), None)

    def _open_session(self, customer: dict) -> dict[str, Any]:
        return {
            "accessToken": f"{ACCESS_TOKEN_PREFIX}{customer['id']}",
            "refreshToken": f"{REFRESH_TOKEN_PREFIX}{customer['id']}",
            "expiresIn": 900,
            "usuario": {
                "id": customer["id"],
                "correo": customer["email"],
                "nombreCompleto": customer.get("nombreCompleto"),
            },
        }


class FakeAccessTokenValidator:
    """Validates only tokens minted by `FakeSecurityAdapter` (`fake-access-*`)."""

    def __init__(self, security: FakeSecurityAdapter):
        self._security = security

    async def validate_customer(self, token: str) -> dict[str, Any]:
        if not token.startswith(ACCESS_TOKEN_PREFIX):
            raise InvalidTokenError("fake validator only accepts fake access tokens")
        claims = self._security.claims_for_token(token)
        if claims is None:
            raise InvalidTokenError("unknown fake access token")
        return claims


class FakeServiceTokenProvider:
    """Mirrors the client_credentials provider without network (A3)."""

    async def get_token(self, *, correlation_id: str | None = None) -> str:
        return SERVICE_TOKEN
