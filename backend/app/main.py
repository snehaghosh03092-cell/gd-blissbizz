from fastapi import FastAPI

from app.api.auth import router as auth_router
from app.api.business_slugs import router as business_slugs_router
from app.api.errors import register_exception_handlers
from app.api.health import router as health_router
from app.api.registrations import router as registrations_router

app = FastAPI(
    title="BlissBiz API",
    version="0.1.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
)
app.include_router(health_router, prefix="/api")
app.include_router(business_slugs_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(registrations_router, prefix="/api/v1")
register_exception_handlers(app)
