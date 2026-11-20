from typing import Annotated, Literal

from email_validator import EmailNotValidError, validate_email
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SecretStr, StringConstraints

from app.schemas.business_slugs import BusinessSlug


def normalize_email(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Enter a valid email address.")
    try:
        result = validate_email(value.strip(), check_deliverability=False, allow_smtputf8=False)
    except EmailNotValidError:
        raise ValueError("Enter a valid email address.") from None
    email = result.ascii_email.lower()
    if len(email) > 254:
        raise ValueError("Email addresses must not exceed 254 characters.")
    return email


Email = Annotated[str, BeforeValidator(normalize_email)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]
Phone = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]


class OwnerRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: Name
    email: Email
    password: SecretStr = Field(min_length=15, max_length=128)
    phone: Phone | None = None


class BusinessRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name
    slug: BusinessSlug
    description: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)] | None
    ) = None
    contact_email: Email | None = None
    contact_phone: Phone | None = None
    address: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None = None
    currency: Literal["INR"] = "INR"


class RegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: OwnerRegistration
    business: BusinessRegistration


class RegisteredOwner(BaseModel):
    id: str
    full_name: str
    email: str


class RegisteredBusiness(BaseModel):
    id: str
    name: str
    slug: str


class BusinessUrls(BaseModel):
    admin: str
    storefront: str


class RegistrationResponse(BaseModel):
    owner: RegisteredOwner
    business: RegisteredBusiness
    urls: BusinessUrls
