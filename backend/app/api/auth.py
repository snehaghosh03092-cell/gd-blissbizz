from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.api.session_cookies import SessionCookies
from app.api.session_dependencies import get_session_service, require_auth_origin
from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.schemas.errors import ErrorResponse
from app.schemas.sessions import CsrfResponse
from app.services.sessions import SessionService

router = APIRouter(prefix="/auth", tags=["owner authentication"])


@router.get(
    "/csrf",
    response_model=CsrfResponse,
    dependencies=[Depends(require_auth_origin)],
    responses={403: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    summary="Start or reuse a session and retrieve its CSRF token",
)
def get_csrf(
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    service: Annotated[SessionService, Depends(get_session_service)],
    config: Annotated[Settings, Depends(get_settings)],
) -> CsrfResponse:
    credentials = service.bootstrap(request.cookies.get(config.session_cookie_name))
    body = CsrfResponse(csrf_token=credentials.record.csrf_token)
    max_age = service.remaining_seconds(credentials.record)
    expires = credentials.record.expires_at
    # Persist successfully before sending any credential to the browser.
    db.commit()
    SessionCookies(config).set(response, credentials.raw_token, expires, max_age)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Vary"] = "Cookie, Origin"
    return body
