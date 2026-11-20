"""One error shape for every service: {"error": {"code", "message", "fields"?, "request_id"?}}."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from blissbiz_common.logging import current_request_id


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        fields: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status, self.code, self.message, self.fields = status, code, message, fields
        self.headers = headers or {}


def error_body(code: str, message: str, fields: dict[str, str] | None = None) -> dict:
    body: dict = {"code": code, "message": message}
    if fields:
        body["fields"] = fields
    request_id = current_request_id()
    if request_id:
        body["request_id"] = request_id
    return {"error": body}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            error_body(exc.code, exc.message, exc.fields),
            status_code=exc.status,
            headers={**exc.headers, "Cache-Control": "no-store"},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = {".".join(str(p) for p in e["loc"][1:]) or "body": e["msg"] for e in exc.errors()}
        return JSONResponse(
            error_body("validation_error", "The request is not valid.", fields), status_code=422
        )

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:  # never leak internals
        return JSONResponse(error_body("internal_error", "Something went wrong."), status_code=500)
