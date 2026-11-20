from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.businesses import BusinessRepository
from app.db.session import get_db
from app.schemas.business_slugs import BusinessSlug, SlugAvailability
from app.schemas.errors import ErrorResponse
from app.services.business_slugs import BusinessSlugService

router = APIRouter(prefix="/business-slugs", tags=["business registration"])


def get_slug_service(db: Annotated[Session, Depends(get_db)]) -> BusinessSlugService:
    return BusinessSlugService(BusinessRepository(db))


@router.get(
    "/{slug}/availability",
    response_model=SlugAvailability,
    responses={422: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    summary="Check a business website address",
    description="An advisory check. A positive result does not reserve the address.",
)
def check_slug_availability(
    slug: BusinessSlug,
    service: Annotated[BusinessSlugService, Depends(get_slug_service)],
) -> SlugAvailability:
    return service.check_availability(slug)
