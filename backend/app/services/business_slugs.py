from app.core.slugs import SlugPolicy
from app.db.businesses import BusinessRepository
from app.schemas.business_slugs import SlugAvailability


class BusinessSlugService:
    def __init__(self, repository: BusinessRepository) -> None:
        self.repository = repository

    def check_availability(self, value: str) -> SlugAvailability:
        slug = SlugPolicy.normalize(value)
        available = not SlugPolicy.is_reserved(slug) and not self.repository.slug_exists(slug)
        return SlugAvailability(slug=slug, available=available)
