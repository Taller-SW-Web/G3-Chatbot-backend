"""Local phone verification with our own OTP (SPEC-04).

The chatbot cannot ask Seguridad to verify the phone this cycle (acuerdo A2),
so it issues, stores and checks its own 6-digit OTP. The rule is exact match:
a verification is valid only for the phone the customer has *today* in
`GET /auth/me`; changing the phone invalidates it automatically.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from src.modules.mod_auth.ports.phone_otp_store_port import PhoneOtpStorePort
from src.modules.mod_auth.ports.sms_sender_port import SmsSenderPort
from src.shared.application.ports.phone_verification_port import PhoneVerificationPort
from src.shared.application.ports.security_port import SecurityPort
from src.shared.application.unit_of_work_port import UnitOfWorkPort
from src.shared.domain.masking import mask_phone


class PhoneVerificationError(Exception):
    def __init__(
        self,
        code: str,
        detail: str,
        *,
        status: int = 400,
        extra: dict[str, Any] | None = None,
    ):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status
        self.extra = extra or {}


def _hash_code(secret: str, customer_id: uuid.UUID, phone: str, code: str) -> str:
    payload = f"{customer_id}:{phone}:{code}".encode()
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


class ManagePhoneVerificationUseCase:
    def __init__(
        self,
        security: SecurityPort,
        otp_store: PhoneOtpStorePort,
        sms: SmsSenderPort,
        verifications: PhoneVerificationPort,
        uow: UnitOfWorkPort,
        *,
        secret: str,
        ttl_seconds: int = 300,
        max_attempts: int = 3,
        max_sends: int = 3,
        send_window_seconds: int = 900,
        modo_qa: bool = False,
        clock=lambda: datetime.now(timezone.utc),
    ):
        self._security = security
        self._otp = otp_store
        self._sms = sms
        self._verifications = verifications
        self._uow = uow
        self._secret = secret
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_attempts = max_attempts
        self._max_sends = max_sends
        self._window = timedelta(seconds=send_window_seconds)
        self._modo_qa = modo_qa
        self._clock = clock

    async def current_phone(self, access_token: str) -> str | None:
        """The phone in `GET /auth/me` today; `None` when the profile has none."""
        profile = dict(await self._security.me(access_token))
        phone = profile.get("celular") or profile.get("telefono")
        return str(phone) if phone else None

    async def status(self, access_token: str, customer_id: uuid.UUID) -> dict[str, Any]:
        phone = await self.current_phone(access_token)
        verified = bool(
            phone and await self._verifications.existe(customer_id, phone)
        )
        return {
            "celularEnmascarado": mask_phone(phone or ""),
            "celularVerificado": verified,
        }

    async def request_otp(self, access_token: str, customer_id: uuid.UUID) -> dict[str, Any]:
        phone = await self.current_phone(access_token)
        if not phone:
            raise PhoneVerificationError(
                "CELULAR_NO_DISPONIBLE",
                "Tu perfil no tiene un celular registrado. Actualízalo en tu cuenta.",
                status=409,
            )
        now = self._clock()
        since = now - self._window
        sent = await self._otp.contar_desde(customer_id, since)
        if sent >= self._max_sends:
            first = await self._otp.primer_envio_desde(customer_id, since)
            retry_after = (
                max(1, int((first + self._window - now).total_seconds()))
                if first is not None
                else int(self._window.total_seconds())
            )
            raise PhoneVerificationError(
                "LIMITE_REENVIOS",
                "Espera unos minutos antes de pedir otro código.",
                status=429,
                extra={"reintentarEn": retry_after},
            )

        code = f"{secrets.randbelow(1_000_000):06d}"
        expires_at = now + self._ttl
        await self._otp.invalidar_pendientes(customer_id, phone)
        await self._otp.crear(
            customer_id,
            phone,
            _hash_code(self._secret, customer_id, phone, code),
            expires_at,
        )
        await self._sms.enviar(phone, code)
        await self._uow.commit()
        answer: dict[str, Any] = {
            "enviado": True,
            "celularEnmascarado": mask_phone(phone),
            "expiraEn": expires_at.isoformat(),
        }
        if self._modo_qa:
            answer["codigo"] = code
        return answer

    async def verify_otp(
        self, access_token: str, customer_id: uuid.UUID, code: str
    ) -> dict[str, Any]:
        phone = await self.current_phone(access_token)
        if not phone:
            raise PhoneVerificationError(
                "CELULAR_NO_DISPONIBLE",
                "Tu perfil no tiene un celular registrado. Actualízalo en tu cuenta.",
                status=409,
            )
        challenge = await self._otp.ultimo_vigente(customer_id, phone)
        if challenge is None:
            raise PhoneVerificationError(
                "CODIGO_VENCIDO", "El código venció. Pide uno nuevo.", status=410
            )
        if challenge.expires_at <= self._clock():
            await self._otp.consumir(challenge.id)
            await self._uow.commit()
            raise PhoneVerificationError(
                "CODIGO_VENCIDO", "El código venció. Pide uno nuevo.", status=410
            )
        if challenge.attempts >= self._max_attempts:
            raise PhoneVerificationError(
                "OTP_INTENTOS_AGOTADOS",
                "Agotaste los intentos. Pide un código nuevo.",
                status=401,
                extra={"intentosRestantes": 0},
            )
        expected = _hash_code(self._secret, customer_id, phone, code)
        if not hmac.compare_digest(challenge.code_hash, expected):
            attempts = await self._otp.incrementar_intentos(challenge.id)
            remaining = max(0, self._max_attempts - attempts)
            await self._uow.commit()
            raise PhoneVerificationError(
                "CODIGO_INVALIDO",
                "Código incorrecto.",
                status=401,
                extra={"intentosRestantes": remaining},
            )
        await self._otp.consumir(challenge.id)
        if not await self._verifications.existe(customer_id, phone):
            await self._verifications.guardar(customer_id, phone)
        await self._uow.commit()
        return {
            "celularVerificado": True,
            "celularEnmascarado": mask_phone(phone),
        }
