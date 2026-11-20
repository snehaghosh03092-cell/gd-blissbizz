"""Signing HTTP client for service-to-service calls (docs/02 section 4.1).

Timeouts are short; only idempotent GETs are retried; a down service becomes a clean 503 envelope.
"""

import asyncio
import json
from urllib.parse import urlencode

import httpx

from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError
from blissbiz_common.logging import REQUEST_ID_HEADER
from blissbiz_common.signing import sign_request

RETRY_STATUS = {502, 503, 504}


class InternalClient:
    def __init__(
        self,
        base_url: str,
        secret: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 5.0,
        retries: int = 2,
        backoff: float = 0.1,
    ):
        self._secret, self._retries, self._backoff = secret, retries, backoff
        self._http = httpx.AsyncClient(
            base_url=base_url, transport=transport, timeout=httpx.Timeout(timeout, connect=2.0)
        )

    async def request(
        self,
        method: str,
        path: str,
        ctx: InternalContext,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        query = f"?{urlencode(params)}" if params else ""
        body = (
            json.dumps(json_body, separators=(",", ":")).encode() if json_body is not None else b""
        )
        attempts = 1 + (self._retries if method.upper() == "GET" else 0)
        last: Exception | None = None
        for attempt in range(attempts):
            headers = sign_request(
                self._secret, method, path + query, body, ctx
            )  # fresh nonce each try
            headers[REQUEST_ID_HEADER] = ctx.request_id
            if body:
                headers["Content-Type"] = "application/json"
            try:
                response = await self._http.request(
                    method, path + query, content=body or None, headers=headers
                )
            except httpx.TransportError as exc:
                last = exc
            else:
                if response.status_code not in RETRY_STATUS or attempt == attempts - 1:
                    return response
                last = None
            await asyncio.sleep(self._backoff * (attempt + 1))
        raise ApiError(503, "service_unavailable", "A required service is unavailable.") from last

    async def aclose(self) -> None:
        await self._http.aclose()


def raise_for(response: httpx.Response) -> dict | None:
    """Return the JSON body of a success; re-raise a downstream error in the shared envelope
    (code, message, per-field details and Retry-After survive the hop)."""
    if response.status_code < 400:
        return response.json() if response.content else None
    fields = None
    try:
        err = response.json()["error"]
        code, message, fields = err["code"], err["message"], err.get("fields")
    except Exception:
        code, message = "upstream_error", "A required service failed."
    headers = {}
    if "retry-after" in response.headers:
        headers["Retry-After"] = response.headers["retry-after"]
    raise ApiError(response.status_code, code, message, fields, headers=headers)
