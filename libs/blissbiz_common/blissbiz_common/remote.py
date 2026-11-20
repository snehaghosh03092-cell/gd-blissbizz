"""Calling other services from a *sync* endpoint (FastAPI runs those in a worker thread).

``SyncRemote.call`` hands the async signing client back to the event loop with
``anyio.from_thread.run`` and returns the JSON body, re-raising downstream errors unchanged.
"""

from functools import partial

import anyio
import httpx

from blissbiz_common.clients import InternalClient, raise_for
from blissbiz_common.context import InternalContext


class SyncRemote:
    def __init__(
        self,
        me: str,
        secret: str,
        urls: dict[str, str],
        transports: dict[str, httpx.AsyncBaseTransport] | None = None,
    ):
        transports = transports or {}
        self.me = me
        self.clients = {
            name: InternalClient(url, secret, transport=transports.get(name))
            for name, url in urls.items()
        }

    def response(
        self, service: str, method: str, path: str, ctx: InternalContext, **kw
    ) -> httpx.Response:
        return anyio.from_thread.run(
            partial(self.clients[service].request, method, path, ctx, **kw)
        )

    def call(self, service: str, method: str, path: str, ctx: InternalContext, **kw) -> dict:
        return raise_for(self.response(service, method, path, ctx, **kw)) or {}

    def service_ctx(self, ctx: InternalContext, business_id: str | None = None) -> InternalContext:
        """How this service identifies itself downstream: a SERVICE actor for one business."""
        return InternalContext(
            request_id=ctx.request_id,
            actor_type="SERVICE",
            business_id=business_id or ctx.business_id,
            actor_id=self.me,
        )

    async def aclose(self) -> None:
        for client in self.clients.values():
            await client.aclose()
