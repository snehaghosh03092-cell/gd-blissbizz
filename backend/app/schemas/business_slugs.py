from typing import Annotated

from pydantic import BaseModel, BeforeValidator

from app.core.slugs import SlugPolicy

BusinessSlug = Annotated[str, BeforeValidator(SlugPolicy.normalize)]


class SlugAvailability(BaseModel):
    slug: BusinessSlug
    available: bool
