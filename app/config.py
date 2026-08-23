from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


INSECURE_DEFAULT_JWT_SECRET = "change-me-in-production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Deployment
    environment: str = "development"  # "development" | "production"

    # Database
    database_url: str = "postgresql+asyncpg://jobsched:jobsched@localhost:5432/jobsched"
    sync_database_url: str = "postgresql+psycopg2://jobsched:jobsched@localhost:5432/jobsched"
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # Auth
    jwt_secret: str = INSECURE_DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24
    # Whether the dashboard session cookie is marked Secure (HTTPS-only).
    # Unset = follow the environment (Secure in production). The explicit
    # false is for a production deployment that genuinely has no TLS in
    # front of it (e.g. an EC2 box reached by bare IP) -- an informed
    # opt-out, not a default; see docs/DEPLOY_EC2.md.
    cookie_secure: bool | None = None

    @property
    def effective_cookie_secure(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.environment == "production"

    # AI failure summaries (optional -- falls back to a rule-based summary
    # when unset, see app/services/ai_summary.py)
    anthropic_api_key: str = ""

    # Worker
    worker_poll_interval_seconds: float = 1.0
    worker_heartbeat_interval_seconds: float = 5.0
    worker_heartbeat_timeout_seconds: float = 20.0
    worker_default_concurrency: int = 4

    # Scheduler
    scheduler_poll_interval_seconds: float = 1.0

    # Watcher
    # DANGER: dev/soak-only escape hatch. True disables the SSRF guard's
    # private-address rejection so watches can target localhost (local
    # demos, the soak benchmark). Never enable in a multi-tenant
    # deployment -- it lets any tenant probe the private network.
    watch_allow_private_targets: bool = False
    # Minimum spacing between fetches to the same host (per worker
    # process). Lowered to 0 by the soak benchmark.
    watch_domain_min_interval_seconds: float = 10.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
