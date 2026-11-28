"""Settings read from environment variables (see .env.template). DATABASE_URL lives in db/connection.py."""
import os
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import urlparse

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


OUTBOX_DISPATCH_MODES = ("direct", "broker")
RABBITMQ_URL_SCHEMES = ("amqp", "amqps")
MODULE_MODES = ("fake", "http")
MODULES = ("security", "products", "sales", "dispatch")
# Modules with an in-process fake or an HTTP adapter wired in the container.
MODULES_WITH_ADAPTERS = ("security", "products", "sales", "dispatch")
PRODUCTION_ENVIRONMENTS = ("production", "prod")
DEV_PHONE_OTP_SECRET = "dev-phone-otp-secret"
DEV_ATTACHMENT_SECRET = "dev-attachment-secret"
DEFAULT_SECURITY_ISSUER = "auth-service"


def _app_env() -> str:
    """Explicit deployment indicator (`APP_ENV`); unset means local development."""
    return os.getenv("APP_ENV", "development").strip().lower() or "development"


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


def _optional_flag(name: str) -> bool | None:
    """Unset/blank inherits the caller's default; otherwise a normal boolean flag."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in ("1", "true", "yes")


def _modules_mode() -> str:
    value = os.getenv("MODULES_MODE", "http").strip().lower()
    if value not in MODULE_MODES:
        raise ValueError(f"MODULES_MODE must be fake or http, got {value!r}")
    return value


def _module_mode_override(module: str) -> str | None:
    """`<MODULE>_MODE` (e.g. SECURITY_MODE); unset or empty inherits MODULES_MODE."""
    name = f"{module.upper()}_MODE"
    value = os.getenv(name, "").strip().lower()
    if not value:
        return None
    if value not in MODULE_MODES:
        raise ValueError(f"{name} must be fake or http, got {value!r}")
    return value


@dataclass(frozen=True)
class Settings:
    # `Secure` flag of the chat_sid cookie. Default on; set COOKIE_SECURE=false for plain-HTTP local dev.
    cookie_secure: bool = True
    # Exact origins allowed to call the mutating session endpoints (CSRF check, ADR-0023).
    allowed_origins: tuple[str, ...] = ("http://localhost:3000",)
    # Shared secret injected by Kong (X-Gateway-Secret) to block direct public backend access (#256).
    gateway_shared_secret: str = ""
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
    # `iss` claim the Seguridad access tokens must carry (real service: its full /auth URL).
    security_expected_issuer: str = DEFAULT_SECURITY_ISSUER
    products_base_url: str = "http://localhost:4011"
    sales_base_url: str = "http://localhost:4012"
    # G5 M2 (Postventa): separate Render service from M1 (Ventas).
    postventa_base_url: str = "http://localhost:8082"
    dispatch_base_url: str = "http://localhost:4013"
    # Seguridad client secret (ADR-0017/A3). No code default: SecurityClient
    # refuses to start without it (set SECURITY_CLIENT_SECRET, see .env.template).
    security_client_secret: str = ""
    # OAuth client id this integration authenticates as for service tokens (A3).
    security_client_id: str = "modulo-chatbot"
    # In-process module fakes for deployed environments without real
    # modules (ADR-0022). Environment wiring belongs to the deploy track.
    modules_mode: str = "http"
    # Per-module overrides of `modules_mode` (None inherits it), so integrations
    # can be switched to the real module one at a time.
    security_mode: str | None = None
    products_mode: str | None = None
    # Public supplier phase 1 is an explicit opt-in; service authentication stays the default.
    products_auth_mode: str = "service"
    sales_mode: str | None = None
    dispatch_mode: str | None = None
    # Deployment indicator (`APP_ENV`). In production the fake Seguridad is
    # refused unless `ALLOW_FAKE_SECURITY=true` opts in (e.g. a demo deploy).
    app_env: str = "development"
    # `api` validates every production guard; `consumer` only needs the outbox/broker
    # configuration and must not depend on attachment storage (backend #222).
    app_role: str = "api"
    allow_fake_security: bool = False
    # Production also refuses the fake Productos/Ventas/Despacho unless this
    # explicit opt-in is set (e.g. a demo deploy). Their in-memory data would
    # otherwise answer as if it were the real service (#255).
    allow_fake_modules: bool = False
    allow_public_products: bool = False
    # Per-module timeouts in seconds (DoD: exact timeouts per integration).
    security_timeout_seconds: float = 5.0
    products_timeout_seconds: float = 4.0
    sales_timeout_seconds: float = 5.0
    postventa_timeout_seconds: float = 5.0
    dispatch_timeout_seconds: float = 4.0
    # Order confirmation e-mail (SPEC-16). Without SMTP_HOST the in-memory
    # FakeEmailSender is used and nothing is delivered. Mailtrap in development.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_sender: str = "Botleta <no-reply@g3-chatbot.local>"
    smtp_starttls: bool = True
    smtp_timeout_seconds: float = 10.0
    # Base of the deep link in the e-mail (`/pedidos/{pedidoId}`).
    frontend_base_url: str = "http://localhost:3000"
    # Send right after enqueueing; the outbox worker then only closes already sent e-mails.
    notification_send_inline: bool = True
    # SPEC-24: internal inbox for suggestions and bugs.
    feedback_inbox_email: str = "soporte@g3-chatbot.local"
    # In-process outbox worker (SPEC-15): polled from the FastAPI lifespan.
    outbox_worker_enabled: bool = True
    outbox_poll_interval_seconds: float = 5.0
    # ADR-0030: `direct` dispatches in process (default and rollback); `broker` relays each
    # outbox row to RabbitMQ. The URL carries credentials: never logged (`repr=False`).
    outbox_dispatch_mode: str = "direct"
    rabbitmq_url: str = field(default="", repr=False)
    rabbitmq_prefetch: int = 10
    # SPEC-14 `ExpirarCheckouts`: sweep period for overdue checkouts (runs with the worker).
    checkout_expiry_interval_seconds: float = 60.0
    # Internal deadline for `POST /checkout` -> Ventas `POST /pedidos`. The frontend/proxy
    # cuts checkout at ~120 s and Render cold starts can exceed it, so 30 s leaves room
    # for the rest of the checkout and turns the timeout into a recoverable 503.
    checkout_sales_deadline_seconds: float = 30.0
    # SPEC-04 legacy mode: the checkout demands a locally verified phone. Opt-in
    # everywhere (`REQUIRE_PHONE_VERIFICATION=true`); production uses the
    # verified-contact guard below instead.
    require_phone_verification: bool = False
    # Default checkout guard: the account e-mail (verified at sign-up, G7) is the
    # verified contact; a locally verified phone is accepted as a fallback.
    # Production defaults to true; development/fake keeps it off.
    require_verified_contact: bool = False
    # SPEC-12: optional address save in Seguridad. A failure never blocks checkout.
    checkout_save_address_enabled: bool = True
    # SPEC-14 Req. 5: the simulator card `4000 0000 0000 3220` waits 5 s when on.
    # Default on outside production (demos and tests) and always off in production:
    # a real deployment must never hold a payment for an artificial delay.
    payment_simulator_latency_enabled: bool = False
    # HMAC key for the OTP hashes. Never a real secret in development.
    phone_otp_secret: str = DEV_PHONE_OTP_SECRET
    phone_otp_ttl_seconds: int = 300
    phone_otp_max_attempts: int = 3
    phone_otp_max_sends: int = 3
    phone_otp_send_window_seconds: int = 900
    # Test-only: return the simulated OTP in the response (SPEC-04 RNF).
    modo_qa: bool = False
    # SPEC-23: private attachment storage (local adapter until Supabase is wired).
    # `local` (default, ephemeral disk) or `supabase` (private bucket, ADR-0028).
    attachment_storage: str = "local"
    # Explicit PO-approved exception to boot production with the local disk (ADR-0028).
    allow_local_attachments: bool = False
    attachment_storage_path: str = "var/attachments"
    supabase_url: str = ""
    supabase_service_key: str = ""
    attachment_bucket: str = "chat-attachments"
    attachment_storage_timeout_seconds: float = 10.0
    attachment_public_base_url: str = "http://localhost:8000/api/v1"
    attachment_signing_secret: str = DEV_ATTACHMENT_SECRET
    attachment_signed_url_ttl_seconds: int = 300
    attachment_max_images_per_turn: int = 6
    attachment_cleanup_interval_seconds: float = 3600.0
    # SPEC-05 retention sweep: closes anonymous conversations (>7d) and archives customer ones (>90d).
    conversation_retention_interval_seconds: float = 3600.0
    # SPEC-19/21 (#265): local post-sale drafts expire after 24 h; the sweep runs with the worker.
    postventa_draft_purge_interval_seconds: float = 3600.0
    attachment_max_bytes: int = 5 * 1024 * 1024
    attachment_max_side: int = 4096
    attachment_stored_side: int = 2048
    attachment_thumbnail_side: int = 320

    def __post_init__(self) -> None:
        if self.outbox_dispatch_mode not in OUTBOX_DISPATCH_MODES:
            raise ValueError(f"OUTBOX_DISPATCH_MODE must be one of {', '.join(OUTBOX_DISPATCH_MODES)}")
        if self.outbox_dispatch_mode == "broker":
            if not self.rabbitmq_url:
                raise ValueError("OUTBOX_DISPATCH_MODE=broker requires RABBITMQ_URL")
            if urlparse(self.rabbitmq_url).scheme not in RABBITMQ_URL_SCHEMES:
                raise ValueError("RABBITMQ_URL must use the amqp:// or amqps:// scheme")
        if self.rabbitmq_prefetch < 1:
            raise ValueError("RABBITMQ_PREFETCH must be >= 1")
        if self.app_role != "consumer":
            if self.attachment_storage not in ("local", "supabase"):
                raise ValueError("ATTACHMENT_STORAGE must be one of local, supabase")
            if self.attachment_storage == "supabase" and not (self.supabase_url and self.supabase_service_key):
                raise ValueError("ATTACHMENT_STORAGE=supabase requires SUPABASE_URL and SUPABASE_SERVICE_KEY")
        if self.products_auth_mode not in ("service", "public"):
            raise ValueError("PRODUCTS_AUTH_MODE must be one of service, public")
        if (
            self.app_env in PRODUCTION_ENVIRONMENTS
            and self.products_auth_mode == "public"
            and not self.allow_public_products
        ):
            raise ValueError(
                "Public Productos in production requires ALLOW_PUBLIC_PRODUCTS=true. "
                "Use PRODUCTS_AUTH_MODE=service for authenticated integration."
            )
        if (
            self.app_env in PRODUCTION_ENVIRONMENTS
            and self.module_mode("security") == "fake"
            and not self.allow_fake_security
        ):
            raise ValueError(
                "Refusing to start with the fake Seguridad (accepts any credentials) when "
                "APP_ENV is production. Set SECURITY_MODE=http, or ALLOW_FAKE_SECURITY=true "
                "to run a demo on purpose."
            )
        fake_modules = [
            module for module in MODULES if module != "security" and self.module_mode(module) == "fake"
        ]
        if self.app_env in PRODUCTION_ENVIRONMENTS and fake_modules and not self.allow_fake_modules:
            names = ", ".join(fake_modules)
            modes = ", ".join(f"{module.upper()}_MODE=http" for module in fake_modules)
            raise ValueError(
                f"Refusing to start with fake modules ({names}) when APP_ENV is production: "
                "they answer with in-memory data instead of the real services. "
                f"Set {modes}, or ALLOW_FAKE_MODULES=true to run a demo on purpose."
            )
        if (
            self.app_role != "consumer"
            and self.app_env in PRODUCTION_ENVIRONMENTS
            and self.attachment_storage == "local"
            and not self.allow_local_attachments
        ):
            raise ValueError(
                "Refusing to start with ATTACHMENT_STORAGE=local when APP_ENV is production: "
                "the container disk is ephemeral and attachments are lost on redeploy (ADR-0028). "
                "Set ATTACHMENT_STORAGE=supabase, or ALLOW_LOCAL_ATTACHMENTS=true for an "
                "explicit, PO-approved exception."
            )

    def module_mode(self, module: str) -> str:
        """Effective `fake`/`http` mode of one module: its override or `modules_mode`."""
        if module not in MODULES:
            raise ValueError(f"unknown module {module!r} ({'/'.join(MODULES)})")
        return getattr(self, f"{module}_mode") or self.modules_mode

    def fake_modules(self) -> tuple[str, ...]:
        return tuple(module for module in MODULES if self.module_mode(module) == "fake")

    @property
    def is_production(self) -> bool:
        return self.app_env in PRODUCTION_ENVIRONMENTS

    def unsafe_defaults(self) -> list[str]:
        """Settings that are only acceptable in development (empty list = production-safe)."""
        problems: list[str] = []
        if self.modo_qa:
            problems.append("MODO_QA=true returns the OTP in the HTTP response")
        if self.phone_otp_secret in ("", DEV_PHONE_OTP_SECRET):
            problems.append("PHONE_OTP_SECRET is unset (public dev constant in use)")
        if self.app_role != "consumer" and self.attachment_signing_secret in ("", DEV_ATTACHMENT_SECRET):
            problems.append("ATTACHMENT_SIGNING_SECRET is unset (public dev constant in use)")
        if not self.cookie_secure:
            problems.append("COOKIE_SECURE=false")
        if self.module_mode("security") == "http" and self.security_expected_issuer in (
            "",
            DEFAULT_SECURITY_ISSUER,
        ):
            problems.append("SECURITY_EXPECTED_ISSUER is missing (default placeholder in use)")
        urls = {
            "FRONTEND_BASE_URL": self.frontend_base_url,
            **{f"ALLOWED_ORIGINS[{o}]": o for o in self.allowed_origins},
        }
        if self.app_role != "consumer":
            urls["ATTACHMENT_PUBLIC_BASE_URL"] = self.attachment_public_base_url
        module_urls = {
            "security": ("SECURITY_BASE_URL", self.security_base_url),
            "products": ("PRODUCTS_BASE_URL", self.products_base_url),
            "sales": ("SALES_BASE_URL", self.sales_base_url),
            "dispatch": ("DISPATCH_BASE_URL", self.dispatch_base_url),
        }
        for module, (name, url) in module_urls.items():
            if self.module_mode(module) == "http":
                urls[name] = url
        if self.module_mode("sales") == "http":
            urls["POSTVENTA_BASE_URL"] = self.postventa_base_url
        for name, url in urls.items():
            if _is_local_url(url):
                problems.append(f"{name} points to localhost")
        if self.module_mode("security") == "fake":
            problems.append("fake Seguridad accepts any credentials")
        return problems


def _origins(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(origin.strip().rstrip("/").lower() for origin in raw.split(",") if origin.strip())


def _is_local_url(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in ("localhost", "127.0.0.1", "0.0.0.0", "::1")


def _is_onrender_host(url: str) -> bool:
    """True only when the URL host is Render, not a lookalike in the path."""
    host = (urlparse(url).hostname or "").lower()
    return host == "onrender.com" or host.endswith(".onrender.com")


def warn_unsafe_defaults(settings: Settings, log) -> None:
    """Startup WARNINGs: development-only settings outside production, plus accepted exceptions."""
    if settings.is_production:
        if settings.attachment_storage == "local":
            log.warning(
                "APP_ENV=%s: ATTACHMENT_STORAGE=local accepted by ALLOW_LOCAL_ATTACHMENTS=true. "
                "Uploads sit on the ephemeral container disk and are lost on every redeploy; "
                "set ATTACHMENT_STORAGE=supabase (ADR-0028).",
                settings.app_env,
            )
        fake_modules = settings.fake_modules()
        if fake_modules:
            log.warning(
                "APP_ENV=%s: fake modules active (%s) accepted by an explicit opt-in. "
                "They answer with in-memory data instead of the real services and /health "
                "reports the channel as degraded (#255).",
                settings.app_env,
                ", ".join(fake_modules),
            )
        if not (settings.require_phone_verification or settings.require_verified_contact):
            log.warning(
                "APP_ENV=%s: REQUIRE_PHONE_VERIFICATION and REQUIRE_VERIFIED_CONTACT are both "
                "false; checkout accepts unverified contacts.",
                settings.app_env,
            )
        if not settings.gateway_shared_secret:
            log.warning(
                "APP_ENV=%s: GATEWAY_SHARED_SECRET is unset; direct requests to the backend "
                "domain are not blocked by GatewaySecretMiddleware (#256).",
                settings.app_env,
            )
        return
    problems = settings.unsafe_defaults()
    if problems:
        log.warning(
            "APP_ENV=%s: development-only settings in use (set APP_ENV=production to enforce): %s",
            settings.app_env,
            "; ".join(problems),
        )


def _require_production_safe(settings: Settings) -> None:
    """Refuse to boot in production with development defaults (the fake Seguridad has its own opt-in)."""
    if not settings.is_production:
        return
    problems = [
        p for p in settings.unsafe_defaults() if not p.startswith("fake Seguridad")
    ]
    if problems:
        raise ValueError(
            "Refusing to start with APP_ENV=production and unsafe settings: " + "; ".join(problems)
        )


@lru_cache
def get_settings(app_role: str = "api") -> Settings:
    settings = _build_settings(app_role)
    _require_production_safe(settings)
    return settings


def _build_settings(app_role: str = "api") -> Settings:
    app_env = _app_env()
    production = app_env in PRODUCTION_ENVIRONMENTS
    require_phone_verification = _optional_flag("REQUIRE_PHONE_VERIFICATION")
    require_verified_contact = _optional_flag("REQUIRE_VERIFIED_CONTACT")
    save_address_enabled = _optional_flag("CHECKOUT_SAVE_ADDRESS_ENABLED")
    simulator_latency_enabled = _optional_flag("PAYMENT_SIMULATOR_LATENCY_ENABLED")
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
        app_env=app_env,
        app_role=app_role,
        allow_fake_security=_flag("ALLOW_FAKE_SECURITY"),
        allow_fake_modules=_flag("ALLOW_FAKE_MODULES"),
        allow_public_products=_flag("ALLOW_PUBLIC_PRODUCTS"),
        security_mode=_module_mode_override("security"),
        products_mode=_module_mode_override("products"),
        products_auth_mode=_choice("PRODUCTS_AUTH_MODE", "service", ("service", "public")),
        sales_mode=_module_mode_override("sales"),
        dispatch_mode=_module_mode_override("dispatch"),
        security_base_url=os.getenv("SECURITY_BASE_URL", "http://localhost:4010").strip().rstrip("/"),
        security_expected_issuer=os.getenv("SECURITY_EXPECTED_ISSUER", DEFAULT_SECURITY_ISSUER).strip() or DEFAULT_SECURITY_ISSUER,
        security_client_secret=os.getenv("SECURITY_CLIENT_SECRET", "").strip(),
        security_client_id=os.getenv("SECURITY_CLIENT_ID", "modulo-chatbot").strip() or "modulo-chatbot",
        products_base_url=os.getenv("PRODUCTS_BASE_URL", "http://localhost:4011").strip().rstrip("/"),
        sales_base_url=(sales_base := os.getenv("SALES_BASE_URL", "http://localhost:4012").strip().rstrip("/")),
        postventa_base_url=(
            postventa_base := os.getenv("POSTVENTA_BASE_URL", "http://localhost:8082").strip().rstrip("/")
        ),
        dispatch_base_url=os.getenv("DISPATCH_BASE_URL", "http://localhost:4013").strip().rstrip("/"),
        security_timeout_seconds=_number("SECURITY_TIMEOUT_SECONDS", "5", float, minimum=0.1),
        products_timeout_seconds=_number("PRODUCTS_TIMEOUT_SECONDS", "4", float, minimum=0.1),
        sales_timeout_seconds=_number(
            "SALES_TIMEOUT_SECONDS",
            "180" if _is_onrender_host(sales_base) else "5",
            float,
            minimum=0.1,
        ),
        postventa_timeout_seconds=_number(
            "POSTVENTA_TIMEOUT_SECONDS",
            "180" if _is_onrender_host(postventa_base) else "5",
            float,
            minimum=0.1,
        ),
        dispatch_timeout_seconds=_number("DISPATCH_TIMEOUT_SECONDS", "4", float, minimum=0.1),
        smtp_host=os.getenv("SMTP_HOST", "").strip(),
        smtp_port=_number("SMTP_PORT", "587", int, minimum=1),
        smtp_username=os.getenv("SMTP_USER", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        smtp_sender=os.getenv("SMTP_FROM", "").strip() or "Botleta <no-reply@g3-chatbot.local>",
        smtp_starttls=os.getenv("SMTP_STARTTLS", "true").strip().lower() != "false",
        smtp_timeout_seconds=_number("SMTP_TIMEOUT_SECONDS", "10", float, minimum=0.1),
        frontend_base_url=os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").strip().rstrip("/"),
        notification_send_inline=os.getenv("NOTIFICATION_SEND_INLINE", "true").strip().lower() != "false",
        feedback_inbox_email=(
            os.getenv("FEEDBACK_INBOX_EMAIL", "soporte@g3-chatbot.local").strip()
            or "soporte@g3-chatbot.local"
        ),
        outbox_worker_enabled=os.getenv("OUTBOX_WORKER_ENABLED", "true").strip().lower() != "false",
        outbox_poll_interval_seconds=_number("OUTBOX_POLL_INTERVAL_SECONDS", "5", float, minimum=0.5),
        outbox_dispatch_mode=_choice("OUTBOX_DISPATCH_MODE", "direct", OUTBOX_DISPATCH_MODES),
        rabbitmq_url=os.getenv("RABBITMQ_URL", "").strip(),
        rabbitmq_prefetch=_number("RABBITMQ_PREFETCH", "10", int, minimum=1),
        checkout_expiry_interval_seconds=_number(
            "CHECKOUT_EXPIRY_INTERVAL_SECONDS", "60", float, minimum=5
        ),
        checkout_sales_deadline_seconds=_number(
            "CHECKOUT_SALES_DEADLINE_SECONDS", "30", float, minimum=1
        ),
        require_phone_verification=(
            require_phone_verification if require_phone_verification is not None else False
        ),
        checkout_save_address_enabled=(
            save_address_enabled if save_address_enabled is not None else True
        ),
        # Non-production defaults on so the latency card is usable in demos/tests;
        # production ignores the override and stays off (issue #249).
        payment_simulator_latency_enabled=(
            False
            if production
            else (simulator_latency_enabled if simulator_latency_enabled is not None else True)
        ),
        require_verified_contact=(
            require_verified_contact if require_verified_contact is not None else production
        ),
        # Production never falls back to another secret: each key must be set on its own.
        phone_otp_secret=(
            os.getenv("PHONE_OTP_SECRET", "").strip()
            or ("" if production else os.getenv("SECURITY_CLIENT_SECRET", "").strip())
            or DEV_PHONE_OTP_SECRET
        ),
        phone_otp_ttl_seconds=_number("PHONE_OTP_TTL_SECONDS", "300", int, minimum=30),
        phone_otp_max_attempts=_number("PHONE_OTP_MAX_ATTEMPTS", "3", int, minimum=1),
        phone_otp_max_sends=_number("PHONE_OTP_MAX_SENDS", "3", int, minimum=1),
        phone_otp_send_window_seconds=_number(
            "PHONE_OTP_SEND_WINDOW_SECONDS", "900", int, minimum=60
        ),
        modo_qa=_flag("MODO_QA"),
        attachment_storage=_choice("ATTACHMENT_STORAGE", "local", ("local", "supabase")),
        allow_local_attachments=_flag("ALLOW_LOCAL_ATTACHMENTS"),
        attachment_storage_path=os.getenv("ATTACHMENT_STORAGE_PATH", "var/attachments").strip(),
        supabase_url=os.getenv("SUPABASE_URL", "").strip().rstrip("/"),
        supabase_service_key=os.getenv("SUPABASE_SERVICE_KEY", "").strip(),
        attachment_bucket=os.getenv("ATTACHMENT_BUCKET", "chat-attachments").strip() or "chat-attachments",
        attachment_storage_timeout_seconds=_number(
            "ATTACHMENT_STORAGE_TIMEOUT_SECONDS", "10", float, minimum=0.5
        ),
        attachment_public_base_url=(
            os.getenv("ATTACHMENT_PUBLIC_BASE_URL", "http://localhost:8000/api/v1").strip().rstrip("/")
        ),
        attachment_signing_secret=(
            os.getenv("ATTACHMENT_SIGNING_SECRET", "").strip()
            or ("" if production else os.getenv("PHONE_OTP_SECRET", "").strip())
            or ("" if production else os.getenv("SECURITY_CLIENT_SECRET", "").strip())
            or DEV_ATTACHMENT_SECRET
        ),
        attachment_signed_url_ttl_seconds=_number(
            "ATTACHMENT_SIGNED_URL_TTL_SECONDS", "300", int, minimum=30
        ),
        attachment_max_images_per_turn=_number(
            "ATTACHMENT_MAX_IMAGES_PER_TURN", "6", int, minimum=1
        ),
        attachment_cleanup_interval_seconds=_number(
            "ATTACHMENT_CLEANUP_INTERVAL_SECONDS", "3600", float, minimum=60
        ),
        gateway_shared_secret=os.getenv("GATEWAY_SHARED_SECRET", "").strip(),
        conversation_retention_interval_seconds=_number(
            "CONVERSATION_RETENTION_INTERVAL_SECONDS", "3600", float, minimum=60
        ),
        postventa_draft_purge_interval_seconds=_number(
            "POSTVENTA_DRAFT_PURGE_INTERVAL_SECONDS", "3600", float, minimum=60
        ),
    )
