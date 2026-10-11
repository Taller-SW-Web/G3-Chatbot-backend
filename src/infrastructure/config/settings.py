"""Settings read from environment variables (see .env.template). DATABASE_URL lives in db/connection.py."""
import os
from dataclasses import dataclass
from functools import lru_cache


def _number(name: str, default: str, cast, *, minimum: float):
    raw = os.getenv(name, default).strip()
    try:
        value = cast(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    # `Secure` flag of the chat_sid cookie. Default on; set COOKIE_SECURE=false for plain-HTTP local dev.
    cookie_secure: bool = True
    # LLM (ADR-0005). The key may be empty: the provider fails lazily, with LLMUnavailableError, when used.
    llm_provider: str = "openai"
    llm_model: str = "gpt-6-luna"
    llm_temperature: float = 0.6
    llm_max_tokens: int = 1024
    llm_timeout_seconds: float = 15.0
    llm_api_key: str | None = None
    llm_vision_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings(
        cookie_secure=os.getenv("COOKIE_SECURE", "true").strip().lower() != "false",
        llm_provider=os.getenv("LLM_PROVIDER", "openai").strip().lower(),
        llm_model=os.getenv("LLM_MODEL", "gpt-6-luna").strip(),
        llm_temperature=_number("LLM_TEMPERATURE", "0.6", float, minimum=0),
        llm_max_tokens=_number("LLM_MAX_TOKENS", "1024", int, minimum=1),
        llm_timeout_seconds=_number("LLM_TIMEOUT_SECONDS", "15", float, minimum=0.1),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip() or None,
        llm_vision_enabled=os.getenv("LLM_VISION_ENABLED", "true").strip().lower() != "false",
    )
