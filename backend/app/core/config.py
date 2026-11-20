import ipaddress
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.origins import normalize_origin


class Settings(BaseSettings):
    database_url: SecretStr
    app_env: Literal["development", "production"] = "development"
    anonymous_session_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    owner_session_ttl_seconds: int = Field(default=28800, ge=60, le=2592000)
    storefront_base_url: str = "http://localhost:5173"
    admin_base_url: str = "http://admin.localhost:5173"
    registration_rate_limit: int = Field(default=5, ge=1, le=1000)
    registration_rate_window_seconds: int = Field(default=900, ge=1, le=86400)
    auth_allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://admin.localhost:5173",
            "http://127.0.0.1:8000",
            "http://localhost:8000",
            "http://admin.localhost:8000",
        ]
    )

    @field_validator("auth_allowed_origins")
    @classmethod
    def validate_origins(cls, origins: list[str]) -> list[str]:
        if not origins:
            raise ValueError("Configure at least one admin origin.")
        return list(dict.fromkeys(normalize_origin(origin) for origin in origins))

    @field_validator("storefront_base_url", "admin_base_url")
    @classmethod
    def validate_website_url(cls, value: str) -> str:
        return normalize_origin(value)

    @field_validator("storefront_base_url")
    @classmethod
    def validate_base_domain(cls, value: str) -> str:
        host = urlsplit(value).hostname
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("Use a DNS base domain, not an IP address.")
        if len(host) > 189 or any(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
            for label in host.split(".")
        ):
            raise ValueError("Use a valid ASCII DNS base domain with room for business subdomains.")
        return value

    @model_validator(mode="after")
    def validate_production(self) -> "Settings":
        if self.app_env == "production" and any(
            not origin.startswith("https://") for origin in self.auth_allowed_origins
        ):
            raise ValueError("Production admin origins must explicitly use HTTPS.")
        if self.app_env == "production":
            if not self.storefront_base_url.startswith(
                "https://"
            ) or not self.admin_base_url.startswith("https://"):
                raise ValueError("Production website URLs must explicitly use HTTPS.")
            if self.admin_base_url not in self.auth_allowed_origins:
                raise ValueError("The production admin URL must be an allowed auth origin.")
            if urlsplit(self.storefront_base_url).hostname.endswith("localhost"):
                raise ValueError("Configure the real production storefront base domain.")
        return self

    @property
    def session_cookie_name(self) -> str:
        return "__Host-blissbiz_session" if self.app_env == "production" else "blissbiz_session"

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()


def get_settings() -> Settings:
    return settings
