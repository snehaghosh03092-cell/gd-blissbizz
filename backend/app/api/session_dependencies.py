from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import csrf_rejected
from app.core.origins import normalize_origin
from app.db.models import BrowserSession
from app.db.session import get_db
from app.db.sessions import SessionRepository
from app.services.sessions import SessionService


class AuthOriginPolicy:
    def __init__(self, settings: Settings) -> None:
        self.allowed_origins = frozenset(settings.auth_allowed_origins)

    def validate(self, request: Request, *, mutation: bool) -> None:
        try:
            target = normalize_origin(f"{request.url.scheme}://{request.headers.get('host', '')}")
            if target not in self.allowed_origins:
                raise csrf_rejected()
            if request.headers.get("sec-fetch-site") == "cross-site":
                raise csrf_rejected()
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            source = None
            if origin is not None:
                source = normalize_origin(origin)
            elif referer is not None:
                source = normalize_origin(referer, allow_path=True)
            if (source is None and mutation) or (
                source is not None and source not in self.allowed_origins
            ):
                raise csrf_rejected()
        except ValueError:
            raise csrf_rejected() from None


def get_session_service(
    db: Annotated[Session, Depends(get_db)], config: Annotated[Settings, Depends(get_settings)]
) -> SessionService:
    return SessionService(SessionRepository(db), config)


def require_auth_origin(
    request: Request, config: Annotated[Settings, Depends(get_settings)]
) -> None:
    AuthOriginPolicy(config).validate(
        request, mutation=request.method not in {"GET", "HEAD", "OPTIONS"}
    )


def require_csrf_session(
    request: Request,
    service: Annotated[SessionService, Depends(get_session_service)],
    config: Annotated[Settings, Depends(get_settings)],
    origin: Annotated[None, Depends(require_auth_origin)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> BrowserSession:
    return service.validate_csrf(request.cookies.get(config.session_cookie_name), csrf_token)


def require_owner_session(
    request: Request,
    service: Annotated[SessionService, Depends(get_session_service)],
    config: Annotated[Settings, Depends(get_settings)],
    origin: Annotated[None, Depends(require_auth_origin)],
) -> BrowserSession:
    return service.require_owner(request.cookies.get(config.session_cookie_name))


def require_owner_mutation(
    owner_session: Annotated[BrowserSession, Depends(require_owner_session)],
    csrf_session: Annotated[BrowserSession, Depends(require_csrf_session)],
) -> BrowserSession:
    """Use on owner-only POST/PATCH/PUT/DELETE endpoints, never a route guard alone."""
    return owner_session
