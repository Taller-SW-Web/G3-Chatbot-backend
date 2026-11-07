"""CallerIdentity: exactamente un sujeto y acceso tipado al cliente (SPEC-05 Req. 1)."""
import uuid

import pytest

from src.domain.value_objects.caller_identity import CallerIdentity

CUSTOMER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def test_customer_devuelve_el_id_autenticado():
    assert CallerIdentity(customer_id=CUSTOMER_ID).customer == CUSTOMER_ID


def test_customer_falla_para_un_anonimo():
    anonimo = CallerIdentity(anonymous_sid="hash")
    with pytest.raises(ValueError):
        _ = anonimo.customer


@pytest.mark.parametrize(
    "customer_id, anonymous_sid",
    [(None, None), (CUSTOMER_ID, "hash")],
)
def test_exige_exactamente_un_sujeto(customer_id, anonymous_sid):
    with pytest.raises(ValueError):
        CallerIdentity(customer_id=customer_id, anonymous_sid=anonymous_sid)
