from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.core.errors import ApiError
from app.schemas.errors import ErrorDetail, ErrorResponse


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        body = ErrorResponse(
            error=ErrorDetail(code=exc.code, message=exc.message, fields=exc.fields)
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=body.model_dump(exclude_none=True),
            headers={**exc.headers, "Cache-Control": "no-store"},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if not request.url.path.startswith("/api/v1/"):
            return await request_validation_exception_handler(request, exc)
        fields = {
            ".".join(str(part) for part in error["loc"][1:]): error["msg"] for error in exc.errors()
        }
        body = ErrorResponse(
            error=ErrorDetail(
                code="validation_error", message="Invalid request data.", fields=fields
            )
        )
        return JSONResponse(status_code=422, content=body.model_dump(exclude_none=True))

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        # Never serialize exception messages: they can contain SQL, credentials, or owner data.
        if not request.url.path.startswith("/api/v1/"):
            return JSONResponse(status_code=503, content={"detail": "Database unavailable"})
        body = ErrorResponse(
            error=ErrorDetail(
                code="database_unavailable", message="Database temporarily unavailable."
            )
        )
        return JSONResponse(
            status_code=503,
            content=body.model_dump(exclude_none=True),
            headers={"Cache-Control": "no-store"},
        )
