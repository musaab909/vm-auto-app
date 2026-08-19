"""Application settings.

Everything credential-shaped is read from the environment. Nothing here carries a
usable default for a secret: `secret_key` has a development-only sentinel that
`check_production_safety()` rejects when `env` is "production".
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SECRET_SENTINEL = "dev-insecure-secret-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="HOSTPLANE_",
        extra="ignore",
    )

    # --- core ---
    env: Literal["development", "test", "production"] = "development"
    debug: bool = False
    secret_key: str = DEV_SECRET_SENTINEL
    base_url: str = "http://localhost:8000"

    # --- database ---
    # Plain str rather than PostgresDsn: pydantic's URL type normalises away the
    # "+psycopg" driver suffix that SQLAlchemy needs to pick its DBAPI.
    database_url: str = "postgresql+psycopg://hostplane:hostplane@localhost:5432/hostplane"
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_echo: bool = False

    # --- broker ---
    redis_url: str = "redis://localhost:6379/0"
    task_always_eager: bool = False

    # --- auth ---
    session_cookie_name: str = "hostplane_session"
    session_max_age_seconds: int = 60 * 60 * 24 * 7
    api_token_prefix: str = "hp"
    login_rate_limit_per_minute: int = 10
    signup_rate_limit_per_hour: int = 5

    # --- hypervisor ---
    driver: Literal["mock", "vsphere"] = "mock"
    vsphere_host: str | None = None
    vsphere_user: str | None = None
    vsphere_password: str | None = None
    vsphere_port: int = 443
    vsphere_verify_ssl: bool = True
    vsphere_ca_bundle: str | None = None
    vsphere_task_timeout_seconds: int = 1800

    # --- console ---
    # "vcenter" hands off to vCenter's own HTML5 console and needs no extra
    # assets. "webmks" serves a self-hosted viewer, which requires VMware's
    # wmks.js from the vSphere HTML Console SDK — it is not redistributable, so
    # the operator supplies it at `console_wmks_path`.
    console_mode: Literal["vcenter", "webmks"] = "vcenter"
    console_wmks_path: str | None = None

    # --- IPAM ---
    ip_quarantine_hours: int = 24

    # --- billing ---
    currency: str = "EUR"
    payment_gateway: Literal["fake"] = "fake"
    gateway_webhook_secret: str = "dev-webhook-secret"
    invoice_due_days: int = 7
    dunning_retry_days: tuple[int, ...] = (1, 3, 7)
    suspend_grace_days: int = 3
    terminate_after_suspend_days: int = 14

    # --- quotas (defaults applied to new accounts) ---
    default_max_instances: int = 5
    default_max_ips: int = 5

    @field_validator("dunning_retry_days", mode="before")
    @classmethod
    def _parse_retry_days(cls, v: object) -> object:
        if isinstance(v, str):
            return tuple(int(part) for part in v.split(",") if part.strip())
        return v

    @property
    def sync_database_url(self) -> str:
        return self.database_url

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    def check_production_safety(self) -> None:
        """Fail fast rather than boot production with development secrets."""
        if not self.is_production:
            return
        problems = []
        if self.secret_key == DEV_SECRET_SENTINEL:
            problems.append("HOSTPLANE_SECRET_KEY is still the development default")
        if self.gateway_webhook_secret == "dev-webhook-secret":
            problems.append("HOSTPLANE_GATEWAY_WEBHOOK_SECRET is still the development default")
        if self.driver == "vsphere" and not self.vsphere_verify_ssl:
            problems.append("HOSTPLANE_VSPHERE_VERIFY_SSL must stay enabled in production")
        if self.debug:
            problems.append("HOSTPLANE_DEBUG must be false in production")
        if problems:
            raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.check_production_safety()
    return settings
