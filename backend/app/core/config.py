from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_JWT_SECRET_PLACEHOLDER = "change-me-in-production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "AbhiAya"
    debug: bool = False

    database_url: str = "postgresql+psycopg://abhiaya:abhiaya@localhost:5432/abhiaya"
    redis_url: str = "redis://localhost:6379/0"

    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    # OpenRouter (OpenAI-API compatible). Kept alongside groq_* so switchback is
    # trivial — see app/services/agent.py for which one is actually wired up.
    openrouter_api_key: str = ""
    openrouter_model: str = "mistralai/mistral-7b-instruct:free"

    wassender_api_key: str = ""
    wassender_instance_id: str = ""
    # Shared secret we append to the webhook URL so we can reject unsolicited calls.
    wassender_webhook_secret: str = ""

    jwt_secret: str = _JWT_SECRET_PLACEHOLDER
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 12

    # Public base URL of THIS backend. Payment links and gateway callbacks are absolute
    # URLs pointing here, so localhost will not work once a real gateway is calling us —
    # use an ngrok/tunnel URL locally.
    public_base_url: str = "http://localhost:8000"

    # --- Payments (none of this exists yet; see docs/PAYMENTS_PLAN.md) ---------------
    jazzcash_merchant_id: str = ""
    jazzcash_password: str = ""
    jazzcash_integrity_salt: str = ""
    jazzcash_post_url: str = (
        "https://sandbox.jazzcash.com.pk/CustomerPortal/transactionmanagement/merchantform"
    )

    easypaisa_store_id: str = ""
    easypaisa_hash_key: str = ""
    easypaisa_post_url: str = "https://easypaystg.easypaisa.com.pk/easypay/Index.jsf"

    # How long a customer has to pay before the order is auto-cancelled.
    payment_expiry_minutes: int = 15

    # Browsers treat http://localhost:3010 and http://127.0.0.1:3010 as DIFFERENT origins,
    # so a dashboard opened on one while only the other is allowlisted fails CORS preflight
    # and the frontend reports a bare "Failed to fetch" with no useful detail. List both.
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:3010",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
        "http://127.0.0.1:3010",
    ]

    # Production origins: comma-separated list of allowed origins set via environment
    # variable. Example value: "https://abhiaya.vercel.app,https://abhiaya-admin.vercel.app"
    # These are merged into cors_origins at runtime (see main.py). Never hardcode here.
    cors_allowed_origins: str = ""

    # In development only, accept any localhost/127.0.0.1 port. Whichever port you end up
    # on after a clash (3000 taken, 3010 taken, …), the dashboard just works. Never used
    # when DEBUG is false — production uses the explicit list above.
    cors_dev_origin_regex: str = r"http://(localhost|127\.0\.0\.1):\d+"

    @model_validator(mode="after")
    def _require_real_jwt_secret_in_production(self) -> "Settings":
        # In production, refuse to start with a missing or placeholder JWT secret —
        # a known default key would let anyone forge admin tokens.
        if not self.debug and (not self.jwt_secret or self.jwt_secret == _JWT_SECRET_PLACEHOLDER):
            raise RuntimeError(
                "JWT_SECRET must be set to a strong random value when DEBUG=false. "
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        return self

    @property
    def effective_cors_origins(self) -> list[str]:
        """Merge the static dev list with any production origins from CORS_ALLOWED_ORIGINS.

        In production (debug=False) this is the only list that applies — no wildcard
        regex. In development the dev regex is also active (see main.py).
        """
        origins = list(self.cors_origins)
        if self.cors_allowed_origins:
            for origin in self.cors_allowed_origins.split(","):
                cleaned = origin.strip().rstrip("/")
                if cleaned and cleaned not in origins:
                    origins.append(cleaned)
        return origins


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
