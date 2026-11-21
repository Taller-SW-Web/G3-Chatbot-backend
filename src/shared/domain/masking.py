"""PII masking shared by the account profile and the order confirmation e-mail."""


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
