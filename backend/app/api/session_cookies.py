from datetime import UTC, datetime

from fastapi import Response

from app.core.config import Settings


class SessionCookies:
    """Keep issuance, rotation, and future logout cookie attributes consistent."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def set(self, response: Response, raw_token: str, expires_at: datetime, max_age: int) -> None:
        response.set_cookie(
            key=self.settings.session_cookie_name,
            value=raw_token,
            max_age=max_age,
            expires=expires_at.replace(tzinfo=UTC),
            path="/",
            httponly=True,
            secure=self.settings.app_env == "production",
            samesite="lax",
        )

    def clear(self, response: Response) -> None:
        response.delete_cookie(
            key=self.settings.session_cookie_name,
            path="/",
            httponly=True,
            secure=self.settings.app_env == "production",
            samesite="lax",
        )
