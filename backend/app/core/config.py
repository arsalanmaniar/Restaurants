from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "AbhiAya"
    debug: bool = False

    database_url: str = "postgresql+psycopg://abhiaya:abhiaya@localhost:5432/abhiaya"
    redis_url: str = "redis://localhost:6379/0"

    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    ultramsg_instance_id: str = ""
    ultramsg_token: str = ""
    # Shared secret we append to the webhook URL so we can reject unsolicited calls.
    ultramsg_webhook_secret: str = ""

    jwt_secret: str = "change-me-in-production"
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

    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:3010",
    ]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
