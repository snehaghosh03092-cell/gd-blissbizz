from dataclasses import dataclass, field

from app.core.errors import ApiError
from app.core.passwords import PasswordService
from app.core.slugs import SlugPolicy
from app.db.businesses import BusinessRepository
from app.db.models import Business, Owner
from app.db.owners import OwnerRepository
from app.schemas.registrations import (
    RegisteredBusiness,
    RegisteredOwner,
    RegistrationRequest,
    RegistrationResponse,
)
from app.services.business_urls import BusinessUrlBuilder
from app.services.sessions import SessionCredentials, SessionService


@dataclass(frozen=True)
class RegistrationResult:
    response: RegistrationResponse = field(repr=False)
    credentials: SessionCredentials = field(repr=False)


class RegistrationService:
    def __init__(
        self,
        owners: OwnerRepository,
        businesses: BusinessRepository,
        sessions: SessionService,
        passwords: PasswordService,
        urls: BusinessUrlBuilder,
    ) -> None:
        self.owners = owners
        self.businesses = businesses
        self.sessions = sessions
        self.passwords = passwords
        self.urls = urls

    def register(self, data: RegistrationRequest, raw_token: str) -> RegistrationResult:
        # Lock and recheck: a competing request may have consumed the CSRF-validated session.
        current = self.sessions.current(raw_token, lock=True)
        if current is None:
            raise ApiError(
                401, "authentication_required", "A valid registration session is required."
            )
        if current.owner_id is not None:
            raise ApiError(
                409, "already_authenticated", "Sign out before registering another owner account."
            )
        slug = data.business.slug
        if SlugPolicy.is_reserved(slug) or self.businesses.slug_exists(slug):
            raise ApiError(
                409,
                "slug_unavailable",
                "This website address is already taken.",
                fields={"business.slug": "Choose another address."},
            )
        if self.owners.email_exists(data.owner.email):
            raise ApiError(
                409,
                "email_unavailable",
                "This email address is already registered.",
                fields={"owner.email": "Use another email or sign in."},
            )
        owner = Owner(
            full_name=data.owner.full_name,
            email=data.owner.email,
            phone=data.owner.phone,
            password_hash=self.passwords.hash(data.owner.password.get_secret_value()),
        )
        self.owners.add(owner)
        business = Business(owner_id=owner.id, **data.business.model_dump())
        self.businesses.add(business)
        credentials = self.sessions.rotate(raw_token, owner.id)
        response = RegistrationResponse(
            owner=RegisteredOwner(id=owner.id, full_name=owner.full_name, email=owner.email),
            business=RegisteredBusiness(id=business.id, name=business.name, slug=business.slug),
            urls=self.urls.for_slug(business.slug),
        )
        return RegistrationResult(response=response, credentials=credentials)
