"""Settings every service shares. Subclass and add service-specific fields."""

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "service"
    app_env: str = Field(default="development", pattern="^(development|production)$")
    log_level: str = "INFO"
    internal_signing_secret: SecretStr = SecretStr("")

    @field_validator("internal_signing_secret")
    @classmethod
    def _strong_enough(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if secret and len(secret) < 32:
            raise ValueError("INTERNAL_SIGNING_SECRET must be at least 32 characters")
        return value
