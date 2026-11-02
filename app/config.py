"""
Central settings, loaded from environment variables (.env).
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./credit_repair.db"

    jwt_secret_key: str = "change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    anthropic_api_key: str = ""

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id_per_report: str = ""

    # Testing only: lets a client unlock their own report for free when no payment
    # processor is configured. Keep False on the live site.
    allow_dev_unlock: bool = False

    # Signed-agreement flow (see routers/agreements.py). Everything is OFF until
    # you set AGREEMENT_REQUIRED=true in Render's Environment tab, so shipping
    # the code does not change how the live site behaves.
    agreement_required: bool = False
    # Credit repair law bars providing paid services until 3 business days after
    # signing (if you are a credit repair organization). OFF by the owner's choice:
    # clients can be unlocked as soon as they have signed and paid, and they can
    # still cancel for 3 business days. Set AGREEMENT_WAITING_PERIOD=true in
    # Render to make unlocks wait until the cancellation period ends.
    agreement_waiting_period: bool = False
    # Principal business address printed in the agreement (required by law in
    # the contract). This is BlackBridge LLC's address and is used only for the
    # main site's organization; a licensee's own address is not set up yet, so
    # licensee clients see "agreement is being finalized" until it is.
    # Override with BUSINESS_ADDRESS in Render if it ever changes.
    business_address: str = "6801 Lake Worth Rd, Lake Worth, FL 33467"
    agreement_timezone: str = "America/New_York"
    # The main site's organization signs as BlackBridge LLC; licensees sign as
    # their own organization name.
    platform_org_slug: str = "blackbridge-office-329"
    platform_legal_name: str = "BlackBridge LLC"

    # Free-preview expiry (see policy.py). 0 = off (the free summary never
    # expires). Set FREE_SCAN_DAYS=3 in Render to make the free summary
    # disappear 3 days after the report was first uploaded, unless it is paid.
    free_scan_days: int = 0
    # "Final after unlock" refund rule (see policy.py). OFF until the owner's
    # attorney approves it. Set REFUND_FINAL_AFTER_UNLOCK=true in Render to turn
    # it on. It only takes effect while AGREEMENT_REQUIRED is false, because the
    # signed agreement promises a 3-business-day cancellation right.
    refund_final_after_unlock: bool = False

    platform_admin_email: str = "you@example.com"
    platform_admin_password: str = "change-me"


settings = Settings()
