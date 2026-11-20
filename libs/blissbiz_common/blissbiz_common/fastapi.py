"""FastAPI glue: every non-public route of a service depends on ``internal_context``."""

from collections.abc import Awaitable, Callable

from fastapi import Request

from blissbiz_common.config import ServiceSettings
from blissbiz_common.context import InternalContext
from blissbiz_common.signing import NonceCache, verify_request


def make_internal_dependency(
    settings: ServiceSettings,
) -> Callable[[Request], Awaitable[InternalContext]]:
    nonces = NonceCache()

    async def internal_context(request: Request) -> InternalContext:
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        body = await request.body()
        return verify_request(
            settings.internal_signing_secret.get_secret_value(),
            request.method,
            target,
            body,
            dict(request.headers),
            nonces,
        )

    return internal_context
