"""Local JWT checks of SPEC-03 Req. 5 (issuer, type, role, JWKS cache)."""
import pytest

from src.adapters.inbound.http.dependencies import jwt_validator
from src.adapters.inbound.http.dependencies.jwt_validator import check_claims, extract_roles
from src.adapters.inbound.http.problem import ProblemError


def test_extract_roles_reads_list_claim():
    assert extract_roles({"roles": ["CLIENTE"]}) == ["CLIENTE"]


def test_check_claims_accepts_customer_access_token():
    claims = {"iss": "auth-service", "tipo": "acceso", "sub": "cust-001",
              "roles": ["CLIENTE"]}
    assert check_claims(claims)["sub"] == "cust-001"


def test_check_claims_rejects_wrong_issuer():
    with pytest.raises(ProblemError) as exc:
        check_claims({"iss": "other", "tipo": "acceso", "sub": "c", "roles": ["CLIENTE"]})
    assert exc.value.code == "TOKEN_INVALIDO"


def test_check_claims_rejects_non_access_token_type():
    with pytest.raises(ProblemError) as exc:
        check_claims({"iss": "auth-service", "tipo": "refresh", "sub": "c",
                      "roles": ["CLIENTE"]})
    assert exc.value.code == "TOKEN_INVALIDO"


def test_check_claims_rejects_seller_without_customer_role():
    """SPEC-03 Req. 1 - Scenario: Usuario sin rol CLIENTE."""
    with pytest.raises(ProblemError) as exc:
        check_claims({"iss": "auth-service", "tipo": "acceso", "sub": "seller-1",
                      "roles": ["VENDEDOR"]})
    assert exc.value.status == 403
    assert exc.value.code == "CUENTA_NO_PERMITIDA"


def test_decode_with_unknown_kid_reports_invalid_token():
    with pytest.raises(ProblemError) as exc:
        jwt_validator.decode_with_jwks("aaa.bbb.ccc", {})
    assert exc.value.code == "TOKEN_INVALIDO"
