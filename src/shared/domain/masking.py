"""PII masking shared by the account profile and the order confirmation e-mail."""


def mask_phone(phone: str) -> str:
    """Masks a Peruvian mobile as `+51 9****4321` (SPEC-04: never show it whole)."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if digits.startswith("51") and len(digits) > 2:
        digits = digits[2:]
    if len(digits) < 4:
        return "+51 9****"
    return f"+51 {digits[0]}****{digits[-4:]}"


def mask_email(email: str) -> str:
    """Masks the local part, keeping the first and last character (`m****a@correo.com`)."""
    local, _, domain = email.strip().partition("@")
    if not domain or not local:
        return "***"
    if len(local) <= 2:
        masked = "*" * len(local)
    else:
        masked = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{masked}@{domain}"
