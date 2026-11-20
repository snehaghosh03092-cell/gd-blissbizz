from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.session_cookies import SessionCookies
from app.api.session_dependencies import get_session_service, require_csrf_session
from app.core.config import Settings, get_settings
from app.core.errors import ApiError
from app.core.passwords import PasswordService
from app.db.businesses import BusinessRepository
from app.db.integrity import is_unique_violation
from app.db.owners import OwnerRepository
from app.db.session import RateLimitSessionLocal, get_db
from app.schemas.errors import ErrorResponse
from app.schemas.registrations import RegistrationRequest, RegistrationResponse
from app.services.business_urls import BusinessUrlBuilder
from app.services.rate_limits import RegistrationRateLimiter
from app.services.registrations import RegistrationService
from app.services.sessions import SessionService

router = APIRouter(tags=["business registration"])


def get_registration_limiter(
    config: Annotated[Settings, Depends(get_settings)],
) -> RegistrationRateLimiter:
    return RegistrationRateLimiter(RateLimitSessionLocal, config)


def get_registration_service(
    db: Annotated[Session, Depends(get_db)],
    config: Annotated[Settings, Depends(get_settings)],
    sessions: Annotated[SessionService, Depends(get_session_service)],
) -> RegistrationService:
    return RegistrationService(
        OwnerRepository(db),
        BusinessRepository(db),
        sessions,
        PasswordService(),
        BusinessUrlBuilder(config),
    )


@router.post(
    "/registrations",
    response_model=RegistrationResponse,
    status_code=201,
    dependencies=[Depends(require_csrf_session)],
    responses={code: {"model": ErrorResponse} for code in (401, 403, 409, 422, 429, 503)},
    summary="Register an owner and business atomically",
)
def register_business(
    data: RegistrationRequest,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    config: Annotated[Settings, Depends(get_settings)],
    service: Annotated[RegistrationService, Depends(get_registration_service)],
    limiter: Annotated[RegistrationRateLimiter, Depends(get_registration_limiter)],
) -> RegistrationResponse:
    limiter.enforce(request.client.host if request.client else "unknown")
    try:
        result = service.register(data, request.cookies[config.session_cookie_name])
        expires = result.credentials.record.expires_at
        max_age = service.sessions.remaining_seconds(result.credentials.record)
        db.commit()
    except IntegrityError as error:
        db.rollback()
        if is_unique_violation(error):
            raise ApiError(
                409, "registration_conflict", "Email or website address is already in use."
            ) from None
        raise
    except (ApiError, SQLAlchemyError):
        db.rollback()
        raise
    SessionCookies(config).set(response, result.credentials.raw_token, expires, max_age)
    response.headers["Cache-Control"] = "no-store"
    return result.response
