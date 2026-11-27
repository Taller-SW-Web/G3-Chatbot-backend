"""G7 Seguridad contract: JWKS (SPEC-03/SPEC-17).

The validator is tested against the documented path and the documented JWKS
document shape. Signing material is generated in the test; the repository never
stores a private key.
"""
import time

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from src.shared.infrastructure.auth.jwks_token_validator import JwksTokenValidator

BASE_URL = "http://mock-security:4010/api/v1"
JWKS_PATH = "/auth/.well-known/jwks.json"


def _key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    public_jwk.update({"kid": "contract-test", "use": "sig", "alg": "RS256"})
    return private_key, public_jwk


def _token(private_key, **overrides):
    now = int(time.time())
    claims = {
        "iss": "auth-service",
        "sub": "11111111-1111-1111-1111-111111111111",
        "tipo": "acceso",
        "roles": ["CLIENTE"],
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "contract-test"})


def test_jwks_fixture_keeps_the_documented_rotation_shape(load_fixture):
    document = load_fixture("g7_jwks_rotacion.json")

    keys = document["keys"]
    assert len(keys) == 2
    assert {key["kid"] for key in keys} == {"2026-10", "2026-09"}
    for key in keys:
        assert key["kty"] == "RSA"
        assert key["use"] == "sig"
        assert key["alg"] == "RS256"
        assert key["e"] == "AQAB"
        assert key["n"]


@respx.mock
@pytest.mark.asyncio
async def test_validator_fetches_the_documented_jwks_path_and_caches_it():
    private_key, public_jwk = _key_pair()
    route = respx.get(f"{BASE_URL}{JWKS_PATH}").mock(
        return_value=httpx.Response(200, json={"keys": [public_jwk]})
    )
    validator = JwksTokenValidator(BASE_URL)

    first = await validator.validate_customer(_token(private_key))
    second = await validator.validate_customer(_token(private_key))

    assert first["sub"] == "11111111-1111-1111-1111-111111111111"
    assert first["roles"] == ["CLIENTE"]
    assert second["tipo"] == "acceso"
    assert route.call_count == 1
