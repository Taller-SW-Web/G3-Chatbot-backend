"""Settings read from environment variables (see .env.template). DATABASE_URL lives in db/connection.py."""
import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    # `Secure` flag of the chat_sid cookie. Default on; set COOKIE_SECURE=false for plain-HTTP local dev.
    cookie_secure: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings(cookie_secure=os.getenv("COOKIE_SECURE", "true").strip().lower() != "false")
