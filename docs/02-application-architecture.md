# 02 · Application architecture (microservices)

> **Scope reduction (2026-11-18):** no SMS, e-mail or payments, so **no notification service** (gateway + 7 services). Sales supports cash on delivery only. Sections mentioning payments, webhooks or notification delivery describe what is *not* being built.

Status: **draft for review, revised 2026-11-18** (replaces the earlier modular-monolith design). Scope decisions from the owner: **local-only for now** (no deployment work), **microservices**, payments **deferred** (local Fake provider), email via **GoDaddy**, SMS **unresolved** (see doc 09), **one MySQL database** (the existing `blissbiz` DB).

## 1. Shape of the system

```mermaid
flowchart LR
  subgraph Browser
    FE[Vue apps<br/>marketing/onboarding · admin · storefront · platform]
  end
  FE -->|/api/v1/*| GW[gateway :8000<br/>edge / BFF]
  GW -->|signed internal calls| ID[identity :8101]
  GW --> ST[store :8102]
  GW --> CA[catalog :8103]
  GW --> SU[supply :8104]
  GW --> SA[sales :8105]
  GW --> BI[billing :8106]
  GW --> AG[agent :8107]
  GW --> NO[notification :8108]
  SA -->|reserve / commit / release| SU
  SA -->|price quote| CA
  SA -->|tax quote| BI
  AG -->|read-only views| DB[(MySQL 8.4<br/>database: blissbiz)]
  ID & ST & CA & SU & SA & BI & NO --> DB
  SU & SA & BI & ID -.outbox events.-> DB
  NO -->|SMTP| MAIL[GoDaddy mailbox SMTP]
  AG --> CLAUDE[Claude text LLM (optional)]
```

Only the **gateway** is reachable from the browser. Every other service listens on localhost (or a private network) and accepts only requests signed by the gateway or by another service.

## 2. Service catalogue

| Service | Port | Owns (tables) | Responsibility | Talks to |
|---|---|---|---|---|
| **gateway** | 8000 | none | Host → experience classification, session resolution, CSRF/origin checks, rate limiting, tenant resolution, request id, routing by path prefix, payment-webhook ingress, platform-admin BFF | identity (sessions), store (domains), all others |
| **identity** | 8101 | `owners`, `businesses`, `business_members`, `business_tax_profiles`, `customers`, `customer_addresses`, `browser_sessions`, `rate_limit_buckets`, `onboarding_drafts`, `onboarding_messages` | Accounts, tenants (business registry), roles, sessions (issue/rotate/revoke/resolve), registration/login, onboarding drafts and validators (GSTIN etc.). **This is today's `backend/` code**, so registration stays one local transaction | – |
| **store** | 8102 | `storefronts`, `store_policies`, `policy_documents`, `domains`, `deployments`*, `deployment_logs`*, `health_checks`* | Storefront config, templates, policies, host → business mapping. *Deployment tables exist but their features are deferred | identity (business lookup) |
| **catalog** | 8103 | `categories`, `products`, `product_variants`, `product_images`, `catalog_imports` | Catalog CRUD, publishing rules, search, images/imports, price/tax-class quotes | – |
| **supply** | 8104 | `warehouses`, `inventory_balances`, `inventory_transactions`, `stock_reservations`, `stock_adjustments`(+lines), `stock_transfers`(+lines), `reconciliation_runs`, `reconciliation_discrepancies`, `suppliers`, `purchase_orders`(+items), `goods_receipts`(+items), `documents`, `purchase_invoices`, `purchase_returns`, `purchase_debit_notes`, `supplier_payments` | Stock ledger and reservations (**the only writer of stock**), purchasing, supplier invoice capture, reconciliation, low-stock detection | – |
| **sales** | 8105 | `carts`, `cart_items`, `orders`, `order_items`, `order_status_history`, `shipments`, `return_requests`(+items), `payments`, `payment_webhook_events`, `refunds` | Cart, checkout saga, orders, fulfilment, returns, payments (provider interface; Fake locally) | catalog, billing (quote), supply (reserve/commit/release) |
| **billing** | 8106 | `tax_rate_versions`, `invoice_sequences`, `invoices`, `invoice_lines`, `credit_notes`(+lines) | Tax engine (pure), invoice issue/immutability, credit notes, tax reports | – (consumes events) |
| **agent** | 8107 | `agent_conversations`, `agent_messages`, `agent_tool_calls`, `voice_logs`, `analysis_runs`, `recommendations`, `recommendation_events` | Onboarding/shopper/owner/insights agents, recommendations and dashboard queries; **read-only access to `rpt_*` views** | identity (draft patches), optional Claude text reasoning |
| **notification** | 8108 | `notifications`, `message_deliveries`, `notification_templates` | In-app notifications, email (GoDaddy SMTP) and SMS (provider interface) delivery, retries | identity (recipients) |

**Shared platform tables** (written by every service, each row stamped with `service`; administered through the gateway's platform-admin routes): `audit_events`, `idempotency_keys`, `jobs`, `outbox_events`, `processed_events`, `error_events`, `integration_configs`.

Why these boundaries: each service owns data that changes together and has one reason to change. Identity keeps accounts, tenants and sessions together so registration/login remain single transactions (a hard-won lesson in `memory.md`). Supply keeps the ledger, balances and reservations together because they must commit atomically. Sales owns the order lifecycle and payments. Billing is separate because invoices are legally immutable and numbering must be gapless. Agent and notification are isolated because they have different failure, cost and secret profiles.

## 3. Data ownership rules (shared database, owned tables)

One MySQL database is kept (existing `blissbiz` DB, Alembic migrations) because of the owner's decision and because the checkout/stock paths need strong consistency. To keep the services genuinely independent:

1. **Only the owning service writes a table.** Others use its API or consume its events.
2. **No cross-service foreign keys**, except to `businesses.id` (the tenant anchor) and the legacy `businesses.owner_id → owners.id` from B00 (both inside identity). Cross-service references (`variant_id` in supply, `customer_id` and `variant_id` in sales, `order_id` in billing/supply) are plain indexed columns, validated through the owner's API at write time and snapshotted where history matters.
3. **No cross-service reads of tables.** The only exception is the agent service, which reads **`rpt_*` views** (a published read model, listed in doc 03 §14) through a read-only database user.
4. **Per-service database users** (`blissbiz_identity`, …) with `GRANT`s limited to owned tables will be generated by a script (P0.5) from `app.db.models.OWNERS`, so violations fail loudly.
5. **Ownership is tested** (`tests/test_schema_rules.py`, implemented): one owner per table, no cross-service foreign keys. The **import-boundary test** (a service may import only its own model module plus platform tables) arrives with the service split (P0.5).
6. Extraction path: giving a service its own database later means moving its tables, dropping the `businesses.id` foreign key, and replacing the `rpt_*` views with events or API calls. No application-level change is needed beyond configuration.

Trade-off acknowledged: a shared database is a pragmatic compromise, not textbook microservices. Rules 1-5 are what stop it degenerating into a distributed monolith.

## 4. Communication

### 4.1 Synchronous (HTTP/JSON, internal)
- Internal base path `/internal/v1/...`, never routed by the gateway to the public.
- **Request signing:** `X-Internal-Signature = HMAC-SHA256(secret, method + path + timestamp + body-hash + context)`, `X-Internal-Timestamp` (±60 s window), and `X-Internal-Context`, a base64 JSON object `{request_id, business_id, actor_type, actor_id, role, permissions[]}` built by the gateway from the verified session and tenant resolution. Services trust **only** that signed context for tenancy and authorization; any client-supplied business/user id in a body is a selector that must match the context.
- **Service-to-service calls** forward the context (user-initiated) or use a service identity (`actor_type=SERVICE`) with an explicit signed `business_id` (jobs, events).
- **Timeouts and retries:** 2 s connect, 5 s read by default; retry only idempotent calls (GET, or POST with `Idempotency-Key`) with jittered backoff (max 2); simple circuit breaker per downstream (open after 5 consecutive failures for 30 s).
- **Contracts:** each service publishes OpenAPI (`/openapi.json`), checked into `contracts/<service>.openapi.json`; a test regenerates it and fails on unreviewed drift; callers use thin typed clients in `libs/blissbiz_common/clients/`.

### 4.2 Asynchronous (events via transactional outbox)
- A service writes domain events to `outbox_events` **in the same transaction** as the state change. A relay loop in the service's worker delivers events to subscribers by `POST /internal/v1/events` (signed), retrying with backoff; consumers record `processed_events(consumer, event_id)` to be idempotent.
- No Kafka/Redis/RabbitMQ: MySQL outbox + HTTP relay is enough locally and removes infrastructure. The transport is behind an interface, so a broker can be added later without changing producers.
- Envelope: `{event_id, type, version, occurred_at, producer, business_id, correlation_id, payload}`. Payloads are self-contained snapshots so consumers don't need to call back.

| Event | Producer | Consumers | Purpose |
|---|---|---|---|
| `business.registered`, `business.activated` | identity | notification, store | Welcome email; create default storefront/policies |
| `customer.registered` | identity | notification | Welcome email |
| `product.published`, `product.unpublished`, `variant.price_changed` | catalog | store, agent (cache version) | Cache invalidation |
| `stock.low`, `stock.out` | supply | notification, agent | Low-stock notifications and recommendations |
| `goods.received`, `reconciliation.discrepancy_found` | supply | notification | Owner alerts |
| `order.placed`, `order.paid`, `order.payment_failed`, `order.cancelled`, `order.shipped`, `order.delivered` | sales | notification, billing (`shipped` by default), agent | Emails; invoice issue; cache invalidation |
| `return.requested`, `refund.issued` | sales | notification, billing | Credit note when an invoice exists |
| `invoice.issued`, `credit_note.issued` | billing | notification, sales | Email invoice link; mark order invoiced |

### 4.3 Orchestration and sagas (no distributed transactions)
- **Checkout** (sales orchestrates): quote prices (catalog) → quote tax (billing) → **reserve** stock (supply, atomic, idempotent, TTL 15 min) → create order + payment in one local transaction → return checkout params. If the local transaction fails after a successful reserve, sales calls `release`; if even that fails, the **reservation TTL is the backstop**, so a crash never strands stock forever.
- **Payment confirmed** (webhook, sales): verify signature → dedupe event → server-side confirmation → call supply `commit` (idempotent, writes ledger SALE) → local transaction marks payment/order paid and writes outbox `order.paid`. If supply is down the webhook returns 5xx and the provider retries; every step is idempotent, so retries converge. Ordering rule: **commit stock first, then mark paid**.
- **Registration/onboarding submit** stays inside identity (one transaction: owner + business + membership + tax profile + session rotation), then emits `business.registered`.
- **Invoice** is event-driven (`order.shipped` → billing issues → `invoice.issued`); a failure leaves the event pending in the outbox and visible in platform admin; billing never blocks shipping.
Details and diagrams: doc 04 §2 and doc 06.

## 5. Shared library (`libs/blissbiz_common`)

One installable package used by all services; it contains no business rules of any single service.
`config` (settings base), `errors` (envelope), `logging` (JSON, request id), `tenancy` (internal context verify/build), `signing` (HMAC helpers), `permissions` (role → permission registry), `idempotency`, `outbox` (writer + relay + consumer dedupe), `jobs` (queue + worker loop), `audit`, `money` (Decimal helpers, rounding policy), `ids`, `clock`, `clients/` (typed internal HTTP clients), `testing/` (factories, fake clock, two-tenant fixture, signed-request helper).

## 6. Repository layout (target)

```text
blissbiz/
  libs/blissbiz_common/…                    shared library (above)
  services/
    gateway/  identity/  store/  catalog/  supply/  sales/  billing/  agent/  notification/
      app/{main.py, api/, services/, db/ (repositories only), schemas/, jobs/}
      tests/  Dockerfile (later)  README.md
  backend/                                  today's code; becomes services/identity in step P0.3
    app/db/models/<service>.py              ONE model module per owning service (implemented)
    migrations/                             single Alembic history for the database
  contracts/                                <service>.openapi.json, events/*.schema.json
  frontend/                                 Vue multi-entry app (admin, storefront, marketing, platform)
  scripts/                                  setup.sh dev_all.sh seed grant_roles.sql
  compose.yaml                              MySQL (+ optional service containers later)
  docs/
```

Inside each service the existing BlissBiz layering stays: thin routers → services (rules) → repositories (own tables only). A service owns its migration slice (its models file), but all migrations live in one Alembic history because there is one database.

## 7. Request lifecycle

```text
browser ─▶ gateway
  1 request id, security headers
  2 host classifier: marketing | admin | platform | storefront (storefront host → store service: domains → business_id; unknown host → 404)
  3 session: cookie → identity /internal/v1/sessions/resolve → {owner|customer|anonymous, csrf_token, memberships}
  4 CSRF + origin check for mutations; rate limit (named scopes)
  5 build signed internal context (business_id proven by membership or Host, role, permissions)
  6 route by prefix → target service (timeouts, retries for safe calls)
 service
  7 verify signature + timestamp; read context only
  8 permission dependency → idempotency dependency → service logic → repositories (business_id everywhere) → commit
  9 outbox events written in the same transaction; audit event for sensitive changes
```
Cross-tenant access still returns **404**; the gateway never forwards a request whose proven business does not match the path selector.

## 8. Gateway routing table

| Public path prefix | Target | Notes |
|---|---|---|
| `/api/v1/auth/*`, `/api/v1/registrations`, `/api/v1/business-slugs/*`, `/api/v1/onboarding/*`, `/api/v1/storefront/auth/*`, `/api/v1/storefront/me/*`, `/api/v1/admin/businesses/{id}/members*`, `/api/v1/admin/businesses` (GET, PATCH detail), `/api/v1/admin/businesses/{id}/tax-profile` | identity | |
| `/api/v1/templates`, `/api/v1/admin/businesses/{id}/storefront*`, `/policies*`, `/domains*`, `/api/v1/storefront` (config), `/api/v1/storefront/policies` | store | deployments deferred |
| `/api/v1/admin/businesses/{id}/{categories,products}*`, `/api/v1/storefront/{categories,products,search}*` | catalog | storefront reads are published-only |
| `/api/v1/admin/businesses/{id}/{warehouses,inventory,suppliers,purchase-orders,goods-receipts,documents,purchase-invoices,purchase-returns,debit-notes,supplier-payments}*` | supply | |
| `/api/v1/storefront/{cart,checkout,orders}*`, `/api/v1/admin/businesses/{id}/{orders,returns}*`, `/api/v1/webhooks/payments/*` | sales | webhook has no cookie/CSRF; signature verified in sales |
| `/api/v1/admin/businesses/{id}/{invoices,reports/tax*}*`, `/api/v1/storefront/orders/{oid}/invoice` | billing | |
| `/api/v1/admin/businesses/{id}/agent`, `/insights/*`, `/api/v1/storefront/agent` | agent | audio uploads streamed through |
| `/api/v1/admin/businesses/{id}/notifications*` | notification | |
| `/api/v1/platform/*` | gateway BFF | aggregates identity/store/jobs/audit; platform role + host required |

Public endpoint semantics are unchanged from doc 05; this table only fixes *which service answers*.

## 9. Background work

Each service runs its own worker process (same code, `python -m app.worker`), claiming rows from the shared `jobs` table by `queue = <service>` with `SELECT … FOR UPDATE SKIP LOCKED`, plus its outbox relay. Jobs: supply (`reservations.expire`, `inventory.reconcile`, `inventory.low_stock_scan`), sales (`payments.reconcile`, `carts.abandon`), billing (`invoice.render_pdf`), agent (`insights.daily_analysis`), notification (`deliveries.send`, retries), identity (`sessions.purge`, `drafts.purge`), gateway (`rate_limits.purge`), all (`idempotency.purge`, `outbox.purge`).

## 10. Frontend

Vue 3 + Vue Router, Vite multi-entry (marketing/onboarding, admin, storefront, platform), shared `src/shared` (API client with CSRF, voice components, UI), composables for state (Pinia only if approved). The frontend speaks only to the gateway (`/api/v1`). Storefront templates are data presets over shared components (not separate apps); the owner's configuration is JSON in `storefronts.draft_config`. Local hosts use `*.localhost` (`admin.localhost:5173`, `<slug>.localhost:5173`), which resolve to 127.0.0.1 in modern browsers.

## 11. Local development topology

| Piece | How |
|---|---|
| MySQL 8.4 | `make db-up` (Docker via Colima on this Mac); `127.0.0.1:3307`, database `blissbiz` |
| Migrations / seed | `make db-migrate`, `make db-seed` (doc 13) |
| All services | `make dev-all` → `scripts/dev_all.sh` starts gateway + 8 services + workers in one terminal with prefixed logs; each service also runs alone with `make dev-<service>` |
| Frontend | `make dev-frontend` (Vite, proxies `/api` to the gateway on :8000) |
| Providers | Fake payments, console sinks for SMS and email unless `GODADDY_SMTP_*` is set (doc 09) |
| Containers for services | Not needed locally; a `Dockerfile` per service and compose profile `full` are added later when deployment is back in scope |

One shared virtualenv for the monorepo keeps local setup simple; each service declares its own dependencies so containers can be built per service later.

## 12. Cross-cutting design points

- **Idempotency, audit, rate limiting, jobs, outbox** are libraries over the shared platform tables, used identically by every service.
- **Observability:** JSON logs with `request_id`, `service`, `business_id`; `/health` (process) and `/ready` (DB + downstream reachability) on every service; the gateway exposes an aggregate view to platform admin.
- **Config:** each service reads its own env (`SERVICE_NAME`, `DATABASE_URL`, `INTERNAL_SIGNING_SECRET`, downstream base URLs); secrets only via environment.
- **Versioning:** internal APIs and events carry versions; additive changes only within a version.
- **Failure behaviour:** gateway returns `503 service_unavailable` with `request_id` if a downstream is down; reads degrade independently (a billing outage does not block browsing or checkout).
- **Scalability note:** services are stateless apart from the database; per-process caches (rate limiter fallback, agent cache) mean one process per service until a shared cache is introduced.

## 13. Risks specific to this architecture

| Risk | Mitigation |
|---|---|
| Operational complexity for a small team | Coarse services (8 + gateway), one repo, one venv, one DB, one command to run everything, shared library |
| Distributed consistency in checkout | Idempotent steps, reservation TTL backstop, ordering rule (stock first), reconciliation jobs, saga tests including failure injection |
| Shared DB erodes boundaries | Ownership rules 1-5, grants per service, import-boundary test, `rpt_*` views only for reads |
| Latency of extra hops | Same-host calls, coarse boundaries, batch endpoints (price/tax quote, multi-line reserve), no per-line calls |
| Internal API sprawl | Contracts checked in; typed clients; tests on drift |
