"""Local JWT checks of SPEC-03 Req. 5 (issuer, type, role, JWKS cache/refresh/throttle).

Real RS256 tokens signed with a throwaway key; the JWKS endpoint is mocked
with respx (no Prism, no network).
"""
import json
import time

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.adapters.outbound.auth.jwks_token_validator import (
    JwksTokenValidator,
    check_claims,
    extract_roles,
)
from src.ports.outbound.module_errors import ModuleUnavailableError
from src.ports.outbound.token_validator_port import AccountNotAllowedError, InvalidTokenError

BASE_URL = "http://seguridad:4010"
JWKS_URL = BASE_URL + "/auth/.well-known/jwks.json"


def _keypair(kid: str = "k1"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = kid
    return private_pem, {"keys": [jwk]}


def _token(
    private_pem: str,
    *,
    kid: str = "k1",
    roles=("CLIENTE",),
    tipo: str = "acceso",
    iss: str = "auth-service",
    sub: str = "cust-001",
    exp_delta: int = 900,
) -> str:
    claims = {"iss": iss, "tipo": tipo, "sub": sub, "roles": list(roles),
              "exp": int(time.time()) + exp_delta}
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": kid})


def test_extract_roles_reads_list_claim():
    assert extract_roles({"roles": ["CLIENTE"]}) == ["CLIENTE"]


def test_check_claims_accepts_customer_access_token():
    claims = {"iss": "auth-service", "tipo": "acceso", "sub": "cust-001",
              "roles": ["CLIENTE"]}
    assert check_claims(claims)["sub"] == "cust-001"


def test_check_claims_rejects_wrong_issuer():
    with pytest.raises(InvalidTokenError):
        check_claims({"iss": "other", "tipo": "acceso", "sub": "c", "roles": ["CLIENTE"]})


def test_check_claims_rejects_non_access_token_type():
    with pytest.raises(InvalidTokenError):
        check_claims({"iss": "auth-service", "tipo": "refresh", "sub": "c",
                      "roles": ["CLIENTE"]})


def test_check_claims_rejects_seller_without_customer_role():
    """SPEC-03 Req. 1 - Scenario: Usuario sin rol CLIENTE."""
    with pytest.raises(AccountNotAllowedError):
        check_claims({"iss": "auth-service", "tipo": "acceso", "sub": "seller-1",
                      "roles": ["VENDEDOR"]})


@respx.mock
@pytest.mark.asyncio
async def test_validate_customer_returns_claims_for_customer_token():
    private_pem, jwks = _keypair()
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks))
    claims = await JwksTokenValidator(BASE_URL).validate_customer(_token(private_pem))
    assert claims["sub"] == "cust-001"


@respx.mock
@pytest.mark.asyncio
async def test_expired_token_is_rejected():
    private_pem, jwks = _keypair()
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks))
    validator = JwksTokenValidator(BASE_URL)
    with pytest.raises(InvalidTokenError):
        await validator.validate_customer(_token(private_pem, exp_delta=-10))


@respx.mock
@pytest.mark.asyncio
async def test_seller_token_raises_account_not_allowed():
    private_pem, jwks = _keypair()
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks))
    validator = JwksTokenValidator(BASE_URL)
    with pytest.raises(AccountNotAllowedError):
        await validator.validate_customer(_token(private_pem, roles=("VENDEDOR",)))


@respx.mock
@pytest.mark.asyncio
async def test_unknown_kid_forces_one_refresh_and_recovers_after_rotation():
    old_pem, old_jwks = _keypair("k1")
    new_pem, new_jwks = _keypair("k2")
    route = respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=old_jwks))
    validator = JwksTokenValidator(BASE_URL)
    await validator.validate_customer(_token(old_pem, kid="k1"))
    route.mock(return_value=httpx.Response(200, json=new_jwks))
    claims = await validator.validate_customer(_token(new_pem, kid="k2"))
    assert claims["sub"] == "cust-001"
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_unknown_kid_refresh_is_throttled():
    """An unauthenticated client cannot force a JWKS download on every request."""
    private_pem, jwks = _keypair("k1")
    route = respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks))
    validator = JwksTokenValidator(BASE_URL)
    await validator.validate_customer(_token(private_pem, kid="k1"))
    after_prime = route.call_count
    for _ in range(3):
        with pytest.raises(InvalidTokenError):
            await validator.validate_customer(_token(private_pem, kid="kX"))
    assert route.call_count == after_prime + 1


@respx.mock
@pytest.mark.asyncio
async def test_jwks_down_still_validates_with_cached_copy():
    private_pem, jwks = _keypair()
    route = respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks))
    validator = JwksTokenValidator(BASE_URL, ttl_seconds=0)
    await validator.validate_customer(_token(private_pem))
    route.mock(side_effect=httpx.ConnectError("down"))
    claims = await validator.validate_customer(_token(private_pem))
    assert claims["sub"] == "cust-001"


@respx.mock
@pytest.mark.asyncio
async def test_jwks_down_without_cache_raises_module_unavailable():
    private_pem, _ = _keypair()
    respx.get(JWKS_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(ModuleUnavailableError):
        await JwksTokenValidator(BASE_URL).validate_customer(_token(private_pem))
