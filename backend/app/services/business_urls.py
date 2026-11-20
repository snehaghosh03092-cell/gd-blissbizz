from urllib.parse import urlsplit

from app.core.config import Settings
from app.core.slugs import SlugPolicy
from app.schemas.registrations import BusinessUrls


class BusinessUrlBuilder:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def for_slug(self, slug: str) -> BusinessUrls:
        slug = SlugPolicy.normalize(slug)
        base = urlsplit(self.settings.storefront_base_url)
        return BusinessUrls(
            admin=f"{self.settings.admin_base_url}/businesses/{slug}",
            storefront=f"{base.scheme}://{slug}.{base.netloc}",
        )
