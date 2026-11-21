"""Local phone OTP rules (SPEC-04): issues, limits, verifies and invalidates."""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.modules.mod_auth.application.manage_phone_verification_use_case import (
    ManagePhoneVerificationUseCase,
    PhoneVerificationError,
)

CUSTOMER = uuid.uuid4()
PHONE = "+51987654321"


class FakeOtpStore:
    def __init__(self):
        self.rows = []

    async def crear(self, customer_id, phone, code_hash, expires_at):
        row = SimpleNamespace(
            id=uuid.uuid4(),
            customer_id=customer_id,
            phone=phone,
            code_hash=code_hash,
            expires_at=expires_at,
            attempts=0,
            consumed_at=None,
            created_at=self.now,
        )
        self.rows.append(row)
        return row

    async def contar_desde(self, customer_id, since):
        return sum(
            1 for row in self.rows
            if row.customer_id == customer_id and row.created_at >= since
        )

    async def primer_envio_desde(self, customer_id, since):
        values = [
            row.created_at for row in self.rows
            if row.customer_id == customer_id and row.created_at >= since
        ]
        return min(values) if values else None

    async def ultimo_vigente(self, customer_id, phone):
        candidates = [
            row for row in self.rows
            if row.customer_id == customer_id and row.phone == phone and row.consumed_at is None
        ]
        return max(candidates, key=lambda row: row.created_at) if candidates else None

    async def invalidar_pendientes(self, customer_id, phone):
        for row in self.rows:
            if row.customer_id == customer_id and row.phone == phone and row.consumed_at is None:
                row.consumed_at = self.now

    async def incrementar_intentos(self, challenge_id):
        row = next(row for row in self.rows if row.id == challenge_id)
        row.attempts += 1
        return row.attempts

    async def consumir(self, challenge_id):
        row = next(row for row in self.rows if row.id == challenge_id)
        row.consumed_at = self.now


class FakeSecurity:
    def __init__(self, phone=PHONE):
        self.phone = phone

    async def me(self, holder_token):
        return {"celular": self.phone}


class FakeSms:
    def __init__(self):
        self.sent = []

    async def enviar(self, phone, code):
        self.sent.append((phone, code))


class FakeVerifications:
    def __init__(self):
        self.rows = set()

    async def existe(self, customer_id, phone):
        return (customer_id, phone) in self.rows

    async def guardar(self, customer_id, phone):
        self.rows.add((customer_id, phone))


class FakeUow:
    async def commit(self):
        return None

    async def rollback(self):
        return None


def build(clock):
    store = FakeOtpStore()
    store.now = clock()
    security = FakeSecurity()
    sms = FakeSms()
    verifications = FakeVerifications()
    service = ManagePhoneVerificationUseCase(
        security=security,
        otp_store=store,
        sms=sms,
        verifications=verifications,
        uow=FakeUow(),
        secret="test-secret",
        ttl_seconds=300,
        max_attempts=3,
        max_sends=3,
        send_window_seconds=900,
        modo_qa=True,
        clock=clock,
    )
    return service, store, sms, verifications, security


@pytest.mark.asyncio
async def test_request_otp_sends_six_digits_and_masks_the_phone():
    now = datetime.now(timezone.utc)
    service, store, sms, _, _ = build(lambda: now)

    answer = await service.request_otp("token", CUSTOMER)

    assert answer["enviado"] is True
    assert answer["celularEnmascarado"] == "+51 9****4321"
    assert len(answer["codigo"]) == 6 and answer["codigo"].isdigit()
    assert sms.sent == [(PHONE, answer["codigo"])]
    assert store.rows[0].code_hash != answer["codigo"]
    assert answer["expiraEn"] == (now + timedelta(minutes=5)).isoformat()


@pytest.mark.asyncio
async def test_more_than_three_sends_in_the_window_is_limited_with_retry_after():
    now = datetime.now(timezone.utc)
    service, store, _, _, _ = build(lambda: now)
    for offset in (0, 60, 120):
        store.now = now + timedelta(seconds=offset)
        await service.request_otp("token", CUSTOMER)

    with pytest.raises(PhoneVerificationError) as exc:
        await service.request_otp("token", CUSTOMER)

    assert exc.value.code == "LIMITE_REENVIOS"
    assert exc.value.status == 429
    assert exc.value.extra["reintentarEn"] > 0


@pytest.mark.asyncio
async def test_correct_code_marks_the_exact_phone_verified():
    now = datetime.now(timezone.utc)
    service, store, _, verifications, _ = build(lambda: now)
    answer = await service.request_otp("token", CUSTOMER)

    result = await service.verify_otp("token", CUSTOMER, answer["codigo"])

    assert result["celularVerificado"] is True
    assert verifications.rows == {(CUSTOMER, PHONE)}
    assert store.rows[0].consumed_at is not None


@pytest.mark.asyncio
async def test_wrong_code_counts_attempts_and_exhausts_after_three():
    now = datetime.now(timezone.utc)
    service, store, _, _, _ = build(lambda: now)
    await service.request_otp("token", CUSTOMER)
    for remaining in (2, 1, 0):
        with pytest.raises(PhoneVerificationError) as exc:
            await service.verify_otp("token", CUSTOMER, "000000")
        assert exc.value.code == "CODIGO_INVALIDO"
        assert exc.value.extra["intentosRestantes"] == remaining
    assert store.rows[0].attempts == 3
    with pytest.raises(PhoneVerificationError) as exc:
        await service.verify_otp("token", CUSTOMER, "000000")
    assert exc.value.code == "OTP_INTENTOS_AGOTADOS"


@pytest.mark.asyncio
async def test_expired_code_is_rejected():
    now = datetime.now(timezone.utc)
    service, store, _, _, _ = build(lambda: now)
    answer = await service.request_otp("token", CUSTOMER)
    store.rows[0].expires_at = now - timedelta(seconds=1)

    with pytest.raises(PhoneVerificationError) as exc:
        await service.verify_otp("token", CUSTOMER, answer["codigo"])
    assert exc.value.code == "CODIGO_VENCIDO"
    assert exc.value.status == 410


@pytest.mark.asyncio
async def test_changing_the_phone_invalidates_the_previous_verification():
    now = datetime.now(timezone.utc)
    service, _, _, verifications, security = build(lambda: now)
    answer = await service.request_otp("token", CUSTOMER)
    await service.verify_otp("token", CUSTOMER, answer["codigo"])
    assert (CUSTOMER, PHONE) in verifications.rows

    security.phone = "+51911222333"
    status = await service.status("token", CUSTOMER)
    assert status["celularVerificado"] is False

    # The old challenge cannot verify the new phone.
    with pytest.raises(PhoneVerificationError) as exc:
        await service.verify_otp("token", CUSTOMER, answer["codigo"])
    assert exc.value.code == "CODIGO_VENCIDO"
