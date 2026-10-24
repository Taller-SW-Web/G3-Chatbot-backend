"""Bearer caller identity wired to the MODULES_MODE token validator (SPEC-03 Req. 5, SPEC-05 Req. 1)."""
import uuid

import pytest
from starlette.requests import Request

from src.adapters.inbound.http.dependencies import jwt_validator
from src.adapters.inbound.http.dependencies.caller_identity import get_caller_identity
from src.adapters.inbound.http.problem import ProblemError
from src.adapters.outbound.fakes.fake_security import FakeAccessTokenValidator
from src.domain.value_objects.caller_identity import CallerIdentity
from src.infrastructure.config.container import provide_security_port
from src.infrastructure.config.settings import Settings

SEED_CUSTOMER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture(autouse=True)
def fresh_validator_cache():
    jwt_validator._default_validator.cache_clear()
    yield
    jwt_validator._default_validator.cache_clear()


def request_with(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw})


def test_validator_dependency_honors_fake_modules_mode(monkeypatch):
    monkeypatch.setattr(jwt_validator, "get_settings", lambda: Settings(modules_mode="fake"))
    validator = jwt_validator.get_access_token_validator()
    assert isinstance(validator, FakeAccessTokenValidator)
    assert jwt_validator.get_access_token_validator() is validator


@pytest.mark.asyncio
async def test_fake_login_token_resolves_the_seeded_customer(monkeypatch):
    settings = Settings(modules_mode="fake")
    monkeypatch.setattr(jwt_validator, "get_settings", lambda: settings)
    session = await provide_security_port(settings).login("cliente@demo.pe", "Demo1234")
    token = session["accessToken"]
    assert token.startswith("fake-access-")
    identity = await get_caller_identity(
        request_with({"Authorization": f"Bearer {token}"}), jwt_validator.get_access_token_validator()
    )
    assert identity == CallerIdentity(customer_id=SEED_CUSTOMER_ID)


@pytest.mark.asyncio
async def test_non_uuid_subject_is_an_invalid_token():
    class BadSubject:
        async def validate_customer(self, token):
            return {"sub": "not-a-uuid"}

    with pytest.raises(ProblemError) as err:
        await jwt_validator.customer_from_bearer("t", BadSubject())
    assert err.value.status == 401 and err.value.code == "TOKEN_INVALIDO"
