"""Settings read from environment variables (see .env.template). DATABASE_URL lives in db/connection.py."""
import os
from dataclasses import dataclass
from functools import lru_cache


LLM_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high")


def _choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = os.getenv(name, default).strip().lower()
    if value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)}, got {value!r}")
    return value


def _number(name: str, default: str, cast, *, minimum: float):
    raw = os.getenv(name, default).strip()
    try:
        value = cast(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def _modules_mode() -> str:
    value = os.getenv("MODULES_MODE", "http").strip().lower()
    if value not in ("fake", "http"):
        raise ValueError(f"MODULES_MODE must be fake or http, got {value!r}")
    return value


@dataclass(frozen=True)
class Settings:
    # `Secure` flag of the chat_sid cookie. Default on; set COOKIE_SECURE=false for plain-HTTP local dev.
    cookie_secure: bool = True
    # Exact origins allowed to call the mutating session endpoints (CSRF check, ADR-0023).
    allowed_origins: tuple[str, ...] = ("http://localhost:3000",)
    # LLM (ADR-0005). The key may be empty: the provider fails lazily, with LLMUnavailableError, when used.
    llm_provider: str = "openai"
    llm_model: str = "gpt-6-luna"
    llm_temperature: float = 0.6
    llm_max_tokens: int = 1024
    llm_timeout_seconds: float = 15.0
    llm_api_key: str | None = None
    llm_vision_enabled: bool = True
    # Reasoning effort sent to the model; "none" keeps tools and temperature available.
    llm_reasoning_effort: str = "none"
    # Name the assistant introduces itself with (SPEC-05 Req. 12; provisional value from the spec).
    assistant_name: str = "Botleta"
    # Module base URLs (ADR-0017): mock or real, switched by config without code changes.
    security_base_url: str = "http://localhost:4010"
    products_base_url: str = "http://localhost:4011"
    sales_base_url: str = "http://localhost:4012"
    dispatch_base_url: str = "http://localhost:4013"
    # Seguridad client secret (ADR-0017/A3). No code default: SecurityClient
    # refuses to start without it (set SECURITY_CLIENT_SECRET, see .env.template).
    security_client_secret: str = ""
    # OAuth client id this integration authenticates as for service tokens (A3).
    security_client_id: str = "modulo-chatbot"
    # In-process module fakes for deployed environments without real
    # modules (ADR-0022). Environment wiring belongs to the deploy track.
    modules_mode: str = "http"
    # Per-module timeouts in seconds (DoD: exact timeouts per integration).
    security_timeout_seconds: float = 5.0
    products_timeout_seconds: float = 4.0
    sales_timeout_seconds: float = 5.0
    dispatch_timeout_seconds: float = 4.0


def _origins(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(origin.strip().rstrip("/").lower() for origin in raw.split(",") if origin.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings(
        cookie_secure=os.getenv("COOKIE_SECURE", "true").strip().lower() != "false",
        allowed_origins=_origins("ALLOWED_ORIGINS", "http://localhost:3000"),
        llm_provider=os.getenv("LLM_PROVIDER", "openai").strip().lower(),
        llm_model=os.getenv("LLM_MODEL", "gpt-6-luna").strip(),
        llm_temperature=_number("LLM_TEMPERATURE", "0.6", float, minimum=0),
        llm_max_tokens=_number("LLM_MAX_TOKENS", "1024", int, minimum=1),
        llm_timeout_seconds=_number("LLM_TIMEOUT_SECONDS", "15", float, minimum=0.1),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip() or None,
        llm_vision_enabled=os.getenv("LLM_VISION_ENABLED", "true").strip().lower() != "false",
        llm_reasoning_effort=_choice("LLM_REASONING_EFFORT", "none", LLM_REASONING_EFFORTS),
        assistant_name=os.getenv("ASSISTANT_NAME", "Botleta").strip() or "Botleta",
        modules_mode=_modules_mode(),
        security_base_url=os.getenv("SECURITY_BASE_URL", "http://localhost:4010").strip().rstrip("/"),
        security_client_secret=os.getenv("SECURITY_CLIENT_SECRET", "").strip(),
        security_client_id=os.getenv("SECURITY_CLIENT_ID", "modulo-chatbot").strip() or "modulo-chatbot",
        products_base_url=os.getenv("PRODUCTS_BASE_URL", "http://localhost:4011").strip().rstrip("/"),
        sales_base_url=os.getenv("SALES_BASE_URL", "http://localhost:4012").strip().rstrip("/"),
        dispatch_base_url=os.getenv("DISPATCH_BASE_URL", "http://localhost:4013").strip().rstrip("/"),
        security_timeout_seconds=_number("SECURITY_TIMEOUT_SECONDS", "5", float, minimum=0.1),
        products_timeout_seconds=_number("PRODUCTS_TIMEOUT_SECONDS", "4", float, minimum=0.1),
        sales_timeout_seconds=_number("SALES_TIMEOUT_SECONDS", "5", float, minimum=0.1),
        dispatch_timeout_seconds=_number("DISPATCH_TIMEOUT_SECONDS", "4", float, minimum=0.1),
    )
