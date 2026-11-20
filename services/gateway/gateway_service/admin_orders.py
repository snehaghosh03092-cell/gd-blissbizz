"""Owner order screens: ``/api/v1/admin/businesses/{business_id}/orders...``.

The business comes from the URL but is only trusted after ``owner_context`` proves membership;
the role's permissions travel in the signed context (``sales.read``, ``sales.fulfil``).
"""

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict, Field


class ShipBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    carrier: str | None = Field(default=None, max_length=60)
    tracking_number: str | None = Field(default=None, max_length=80)


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=255)


def install_admin_order_routes(app: FastAPI, d) -> None:
    base = "/api/v1/admin/businesses/{business_id}/orders"

    @app.get(base)
    async def orders(business_id: str, request: Request, response: Response):
        d.check_origin(request, mutation=False)
        ctx, _ = await d.owner_context(request, business_id)
        d.no_store(response)
        params = d.passthrough(request, "status", "q", "page", "page_size")
        return await d.call(d.sales, "GET", "/internal/v1/orders", ctx, params)

    @app.get(base + "/{order_id}")
    async def order(business_id: str, order_id: str, request: Request, response: Response):
        d.check_origin(request, mutation=False)
        ctx, _ = await d.owner_context(request, business_id)
        d.no_store(response)
        return await d.call(d.sales, "GET", f"/internal/v1/orders/{order_id}", ctx)

    async def act(
        business_id: str,
        order_id: str,
        action: str,
        request: Request,
        response: Response,
        body: dict | None = None,
    ) -> dict:
        await d.require_csrf(request)
        ctx, _ = await d.owner_context(request, business_id)
        d.no_store(response)
        return await d.call(
            d.sales, "POST", f"/internal/v1/orders/{order_id}/{action}", ctx, json_body=body
        )

    @app.post(base + "/{order_id}/pack")
    async def pack(business_id: str, order_id: str, request: Request, response: Response):
        return await act(business_id, order_id, "pack", request, response)

    @app.post(base + "/{order_id}/ship")
    async def ship(
        business_id: str, order_id: str, body: ShipBody, request: Request, response: Response
    ):
        return await act(business_id, order_id, "ship", request, response, body.model_dump())

    @app.post(base + "/{order_id}/deliver")
    async def deliver(business_id: str, order_id: str, request: Request, response: Response):
        return await act(business_id, order_id, "deliver", request, response)

    @app.post(base + "/{order_id}/cancel")
    async def cancel(
        business_id: str, order_id: str, body: CancelBody, request: Request, response: Response
    ):
        return await act(business_id, order_id, "cancel", request, response, body.model_dump())
