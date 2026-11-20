"""Request ids and JSON logs. Never log bodies, cookies, tokens, passwords or personal data."""

import contextvars
import json
import logging
import uuid
from datetime import UTC, datetime

from fastapi import FastAPI, Request

REQUEST_ID_HEADER = "X-Request-ID"
_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)


def current_request_id() -> str | None:
    return _request_id.get()


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": current_request_id(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            entry.update(extra)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(service: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for chatty in ("httpx", "httpcore"):  # one line per internal call is noise, not signal
        logging.getLogger(chatty).setLevel(logging.WARNING)


def install_request_id(app: FastAPI) -> None:
    """Accept a well-formed incoming id (from the gateway) or make one; echo it back."""

    @app.middleware("http")
    async def _middleware(request: Request, call_next):
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        rid = incoming if 8 <= len(incoming) <= 64 and incoming.isascii() else uuid.uuid4().hex
        token = _request_id.set(rid)
        try:
            try:
                response = await call_next(request)
            except Exception:  # last resort: log it, never leak internals, keep the request id
                from fastapi.responses import JSONResponse

                from blissbiz_common.errors import error_body

                logging.getLogger("blissbiz.unhandled").exception("unhandled error")
                response = JSONResponse(
                    error_body("internal_error", "Something went wrong."), status_code=500
                )
        finally:
            _request_id.reset(token)
        response.headers[REQUEST_ID_HEADER] = rid
        return response
