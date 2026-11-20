"""Owner routes for editing (steps 8-11) as one table: catalog editing, stock operations,
purchasing, invoices and returns. ``/api/v1/admin/businesses/{business_id}/<path>``.

Every route: Origin check; CSRF for anything that changes data; the business must be one of the
signed-in owner's memberships (else 404); the role's permissions travel in the signed context and
each service checks the exact permission it needs. Path ids are pattern-checked before they are
put into an internal URL, and JSON bodies are passed through for the service to validate
(unknown fields are rejected there).
"""

import json
import re

from blissbiz_common.clients import raise_for
from blissbiz_common.errors import ApiError
from fastapi import FastAPI, Request, Response

MAX_BODY = 15 * 1024 * 1024  # a 10 MB document is about 13.4 MB as base64 JSON
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# (method, public path, service). Internal path: /internal/v1 + path, with inventory/ -> stock/.
ROUTES = [
    ("POST", "/categories", "catalog"),
    ("PATCH", "/categories/{category_id}", "catalog"),
    ("POST", "/products", "catalog"),
    ("PATCH", "/products/{product_id}", "catalog"),
    ("POST", "/products/{product_id}/publish", "catalog"),
    ("POST", "/products/{product_id}/unpublish", "catalog"),
    ("POST", "/products/{product_id}/archive", "catalog"),
    ("POST", "/products/{product_id}/variants", "catalog"),
    ("PATCH", "/variants/{variant_id}", "catalog"),
    ("POST", "/warehouses", "supply"),
    ("POST", "/inventory/opening", "supply"),
    ("GET", "/inventory/adjustments", "supply"),
    ("POST", "/inventory/adjustments", "supply"),
    ("POST", "/inventory/adjustments/{adjustment_id}/post", "supply"),
    ("GET", "/inventory/transfers", "supply"),
    ("POST", "/inventory/transfers", "supply"),
    ("POST", "/inventory/transfers/{transfer_id}/receive", "supply"),
    ("POST", "/inventory/transfers/{transfer_id}/cancel", "supply"),
    ("GET", "/inventory/reconcile", "supply"),
    ("POST", "/inventory/reconcile", "supply"),
    ("GET", "/suppliers", "supply"),
    ("POST", "/suppliers", "supply"),
    ("PATCH", "/suppliers/{supplier_id}", "supply"),
    ("GET", "/purchase-orders", "supply"),
    ("POST", "/purchase-orders", "supply"),
    ("GET", "/purchase-orders/{po_id}", "supply"),
    ("PUT", "/purchase-orders/{po_id}", "supply"),
    ("POST", "/purchase-orders/{po_id}/approve", "supply"),
    ("POST", "/purchase-orders/{po_id}/cancel", "supply"),
    ("POST", "/purchase-orders/{po_id}/receipts", "supply"),
    ("POST", "/documents", "supply"),
    ("GET", "/purchase-invoices", "supply"),
    ("POST", "/purchase-invoices", "supply"),
    ("GET", "/purchase-invoices/{pi_id}", "supply"),
    ("PATCH", "/purchase-invoices/{pi_id}", "supply"),
    ("POST", "/purchase-invoices/{pi_id}/submit", "supply"),
    ("POST", "/purchase-invoices/{pi_id}/approve", "supply"),
    ("POST", "/purchase-invoices/{pi_id}/reject", "supply"),
    ("GET", "/supplier-payments", "supply"),
    ("POST", "/supplier-payments/{payment_id}/pay", "supply"),
    ("GET", "/invoices", "billing"),
    ("GET", "/invoices/{invoice_id}", "billing"),
    ("GET", "/credit-notes/{note_id}", "billing"),
    ("GET", "/orders/{order_id}/invoice", "sales"),
    ("GET", "/returns", "sales"),
    ("POST", "/returns/{return_id}/approve", "sales"),
    ("POST", "/returns/{return_id}/reject", "sales"),
    ("POST", "/returns/{return_id}/receive", "sales"),
    ("POST", "/returns/{return_id}/refund", "sales"),
    # step 12: guided setup (identity) and the shop's look and rules (store)
    ("GET", "/onboarding", "identity"),
    ("PATCH", "/onboarding", "identity"),
    ("POST", "/onboarding/submit", "identity"),
    ("GET", "/storefront/settings", "store"),
    ("PUT", "/storefront/setup", "store"),
    ("POST", "/storefront/unpublish", "store"),
    ("PUT", "/policies", "store"),
    # step 13: insights (agent)
    ("GET", "/insights/recommendations", "agent"),
    ("POST", "/insights/recommendations/{rec_id}/{action}", "agent"),
    ("GET", "/insights/dashboard", "agent"),
    ("POST", "/insights/simulate", "agent"),
]  # fmt: skip


async def read_json(request: Request) -> dict | list | None:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise ApiError(413, "payload_too_large", "The request is too large.")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        raise ApiError(422, "validation_error", "The request body is not valid JSON.") from None


def install_admin_proxy(app: FastAPI, d) -> None:
    clients = {
        "catalog": d.catalog,
        "supply": d.supply,
        "sales": d.sales,
        "billing": d.billing,
        "identity": d.identity,
        "store": d.store,
        "agent": d.agent,
    }
    prefix = "/api/v1/admin/businesses/{business_id}"

    def make(method: str, service: str, internal: str):
        async def handler(request: Request, response: Response):
            ids = dict(request.path_params)
            business_id = ids.pop("business_id")
            if not all(SAFE_ID.match(v) for v in ids.values()):
                raise ApiError(404, "not_found", "Not found.")
            if method == "GET":
                d.check_origin(request, mutation=False)
            else:
                await d.require_csrf(request)
            ctx, _ = await d.owner_context(request, business_id)
            body = None if method == "GET" else await read_json(request)
            params = (
                dict(request.query_params) if method == "GET" and request.query_params else None
            )
            path = internal.format(**ids)
            upstream = await clients[service].request(
                method, path, ctx, params=params, json_body=body
            )
            data = raise_for(upstream)
            response.status_code = upstream.status_code
            d.no_store(response)
            return data

        handler.__name__ = f"{method.lower()}_{service}_{abs(hash(internal))}"
        return handler

    for method, public, service in ROUTES:
        internal = "/internal/v1" + public.replace("/inventory/", "/stock/", 1)
        app.add_api_route(prefix + public, make(method, service, internal), methods=[method],
                          include_in_schema=True)  # fmt: skip

    @app.get(prefix + "/documents/{document_id}/content")
    async def document(business_id: str, document_id: str, request: Request):
        """The original supplier document, byte for byte (fingerprint checked by supply)."""
        if not SAFE_ID.match(document_id):
            raise ApiError(404, "not_found", "Not found.")
        d.check_origin(request, mutation=False)
        ctx, _ = await d.owner_context(request, business_id)
        upstream = await d.supply.request(
            "GET", f"/internal/v1/documents/{document_id}/content", ctx
        )
        if upstream.status_code >= 400:
            raise_for(upstream)
        headers = {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": upstream.headers.get("content-disposition", "attachment"),
        }
        return Response(
            upstream.content, media_type=upstream.headers.get("content-type"), headers=headers
        )
