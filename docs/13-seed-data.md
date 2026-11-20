# 13 · Seed data guide

Status: **implemented and verified** (2026-11-18). Everything here is fictional development data for **local databases only**.

## 1. Run it

```sh
make db-up            # MySQL in Docker (Colima/Rancher); creates the container once
make db-migrate       # applies migrations 0001-0014 (77 tables, 8 views, 13 triggers)
make db-reseed        # empties the LOCAL database and seeds it (about 3 seconds)
make db-check         # re-runs the 24 integrity checks on whatever is in the database
make db-facts         # prints the ground-truth answers (section 4) for the current data
```

Options (pass with `SEED_ARGS`, for example `make db-reseed SEED_ARGS="--anchor 2026-11-18T10:00"`):

| Option | Meaning |
|---|---|
| `--anchor 2026-11-18T10:00` | Treat this UTC time as "now". Same anchor + same seed = **identical data**. Default: the current minute, so "yesterday" and "last 7 days" are always meaningful. |
| `--scale 0.1` | Activity multiplier (tests use 0.12–0.35). 1.0 is the full dataset. |
| `--seed N` | Change the random choices. |
| `--reset` | Required when the database already has data. Refused for any non-local host. |

The numbers in section 4 were produced with `--anchor 2026-11-18T10:00 --seed 20261118`; the database currently holds exactly that.

## 2. What you get

7 businesses, 18 accounts, 580 customers, 118 products (342 variants), about 4,400 orders over 90 days, 13,700 stock-ledger rows, 4,100 invoices, 137 credit notes, 559 purchase orders, plus notifications, deliveries, audit events, voice logs and platform configuration.

| Business (slug) | Template · city | GST | Prices | Why it exists |
|---|---|---|---|---|
| **Threads & Co** (`threads-co`) | fashion · Bengaluru | registered, regular | tax-inclusive | Many variants (size × colour): 198 sellable variants, two warehouses, returns, stock-outs per size, festive spike, custom domain in `VERIFYING` state |
| **FreshBasket Grocers** (`freshbasket`) | grocery · Pune | registered | tax-inclusive | Fractional units (kg, litre, dozen), 0%/5%/18%/40% mix, high volume, 35% COD, tiny order values |
| **VoltEdge Electronics** (`voltedge`) | electronics · Hyderabad | **unregistered** | tax-inclusive | **Bill of supply, no tax**, slow high-value items, dead stock, many returns |
| **PixelForge Gaming** (`pixelforge`) | gaming · New Delhi | registered | **tax-exclusive** | Tax added on top, 18% on almost everything, high-value orders, launch product, mostly inter-state sales |
| **Spice Route Kitchen** (`spiceroute`) | food · Kolkata | unregistered | tax-inclusive | Pack-size variants, festive season, bill of supply |
| **NewLeaf Studio** (`newleaf`) | fashion · Kochi | – | – | A brand-new business in `READY_FOR_STORE_SETUP`: empty-state questions ("I have no products yet") |
| **Midnight Mart** (`midnight-mart`) | grocery · Jaipur | – | – | `SUSPENDED` by the platform: access-denied and platform-admin scenarios |

### Accounts (one dev password for every account: `REDACTED-EXAMPLE-CREDENTIAL`)

| Who | Login |
|---|---|
| Platform admin | `platform@blissbiz.example` |
| Owner / manager / staff of each business | `owner@<slug>.example`, `manager@<slug>.example`, `staff@<slug>.example` (newleaf and midnight-mart have an owner only) |
| Customers (per business) | `firstname.lastnameN@example.com`, for example `aarav.bhatt86@example.com` (a Threads & Co customer) |

The dev password is documented because these accounts exist only in local databases. Never reuse it.

### Product personas (demand and stock behaviour)

Each product has a persona that the simulator turns into sales history and stock, so a catalog is not uniform:

| Persona | Behaviour | Question it enables |
|---|---|---|
| BEST / STEADY | Healthy demand, automatically restocked | "What are my top sellers?" |
| SLOW | Low demand, large overstock | "What is overstocked?" |
| LOW | Stock at or below the reorder level, PO in transit | "What is running low?" |
| STOCKOUT | Sold out some days ago, PO overdue | "What is out of stock?" "Why?" |
| DEAD | No sales for 65+ days, stock remains | "What isn't selling?" |
| NEW / LAUNCH | Launched 12 / 8 days ago; LAUNCH sells out fast | "How is the new product doing?" |
| SEASONAL | Demand ramping up (festive, winter) | "What is trending up?" |
| DECLINING | Demand falling across the period | "What is trending down?" |
| DRAFT / ARCHIVED | Unpublished / discontinued with stock | Shopper must not see them; owner can |

## 3. How the data is built (so you can trust it)

A day-by-day simulator (`backend/app/seed/simulate.py`) plays each business forward from 91 days ago:

1. **Opening stock** for every variant, sized from the persona.
2. **Orders** each day: Poisson counts with weekday, growth and a festive-sale boost; customers (repeat and new) choose lines weighted by persona, size and colour popularity; items without stock are simply not bought (so stock-outs are real).
3. **Reservations → payment → fulfilment**: stock is reserved at checkout (online orders commit to the ledger on payment, COD at dispatch), then packed, shipped, delivered; some payments fail, some orders are cancelled, some are returned (partial or full) with refunds and **credit notes**.
4. **Restocking**: when stock plus incoming falls to twice the threshold, a purchase order is raised to the supplier, delivered after its lead time (some late, some partial) via goods receipts and ledger entries.
5. **Documents**: purchase invoices (82% approved, the rest awaiting review or draft with low-confidence extracted fields), supplier payments (some overdue), **gapless invoice numbers** per business per financial year, immutable invoice rows.
6. **Everything flows through one ledger**, so balances equal the ledger by construction.

### The 24 integrity checks (all pass on every seed; also enforced by tests)

Ledger sums equal balances · every ledger group has a balance · reserved equals active reservations · no negative availability · order totals add up for inclusive and exclusive pricing · invoice equals its lines and its order · invoice numbers gapless · sequence rows match · credit notes ≤ invoice · refunds ≤ captured payment · child rows belong to the parent's business (items, variants, ledger warehouses, ledger variants, order customers, order variants) · invoices only for shipped-or-later orders · online confirmed orders have a captured payment · PO totals equal item sums · received ≤ ordered · returns ≤ ordered · published products have an active variant · GST-registered businesses have a GSTIN.

### Database-level protections (verified on MySQL)

Updating or deleting a ledger row, an audit event, an invoice line or a credit note is **rejected by a trigger**; changing an issued invoice's amounts, parties or numbers is rejected (only `status` and the rendered-PDF path may change); `reserved > on_hand` and negative stock are rejected by CHECK constraints; a SKU repeats across businesses but never within one.

## 4. Answer key (anchor `2026-11-18T10:00` UTC, seed `20261118`)

"Today" is 2026-11-18. Use these to test agents; each answer comes from the `rpt_*` read views (the agent's only data source). Reproduce any of them with `make db-facts`.

### Shopper questions (storefront of `threads-co`)

| Question | Answer |
|---|---|
| "Show me black items under 1000 rupees" | 19 in-stock variants across 6 products: Classic Crew T-Shirt, Cotton Socks (Pack of 3), Cotton Straight Kurta, Graphic Print Tee, Leather Belt, Oversized Drop-Shoulder Tee |
| "Do you have size M?" | Yes: 38 in-stock size-M variants |
| "What is the cheapest running shoe?" | Running Shoes, ₹2,499 (only one product; sizes UK6–UK10) |
| "Do you have the black bodycon dress?" | No: all four sizes (XS, S, M, L) are sold out |
| "Where is my latest order?" | Needs a customer login; answered from that customer's own orders |

### Owner questions

| Business | Question | Answer |
|---|---|---|
| threads-co | How many Classic Crew T-Shirts are left? | 103 units across sizes and colours |
| threads-co | What were my sales yesterday? | 11 orders, ₹48,035, 33 units |
| threads-co | Top 5 products this month? | Classic Crew T-Shirt (137), Cotton Socks Pack of 3 (107), Cotton Straight Kurta (103), Slim Fit Denim Jeans (98), Canvas Sneakers (91) |
| threads-co | Which category earns the most? | Kurtas ₹3.30 lakh, then Jeans ₹2.58 lakh, Footwear ₹2.16 lakh |
| threads-co | Which orders are pending? | 2 confirmed, 10 packed, 4 awaiting payment |
| threads-co | What is running low / out of stock? | 28 low-stock variants; out of stock includes all four Black Bodycon Dress sizes |
| threads-co | What isn't selling? | Checked Flannel Shirt (32 units), Formal Cotton Shirt (34), Leather Loafers (24), Polo T-Shirt (24), Leather Biker Jacket (15), Denim Jacket (12)… |
| threads-co | What is the value of my stock? | ₹12.02 lakh at cost |
| threads-co | Which purchase orders are overdue? | PO-00231 Tiruppur Knits (due 12 Nov), PO-00199 Agra Footwear Co (due 16 Nov) |
| threads-co | Supplier payments overdue? | 30 payments, ₹1.56 lakh |
| threads-co | Output GST for the last 30 days? | CGST ₹25,753.84, SGST ₹25,751.36, IGST ₹55,121.24 (CGST/SGST differ by paise from odd-rupee splits) |
| freshbasket | How many litres of milk are left? | 78 |
| freshbasket | Revenue last 7 days vs the 7 before? | ₹48,492 vs ₹1,22,346 (the earlier week included the festive sale) |
| freshbasket | What is out of stock? | Dishwash Liquid 500 ml, Paneer 200 g, Salted Butter 100 g, Spinach Bunch |
| freshbasket | COD or online? | 557 COD, 1,032 online |
| voltedge | Do I charge GST? | No: unregistered, 461 bills of supply and no tax lines |
| voltedge | Dead stock? | Mechanical Keyboard (11), USB Hub 4-Port (11) |
| pixelforge | Is tax added to prices? | Yes: prices are tax-exclusive; 18% added; 416 of 634 invoices are inter-state (IGST) |
| pixelforge | Out of stock? | Capture Card 4K, Limited Edition Controller (launch sell-out), Streaming Mic USB |
| spiceroute | Top sellers? | Assam CTC Tea, Cow Ghee, Garam Masala, Mixture Namkeen, Gulab Jamun Mix |
| newleaf | How many products do I have? | None yet (empty-state answer) |

### Platform-admin questions

| Question | Answer |
|---|---|
| Businesses by status? | 5 active, 1 onboarding (`newleaf`), 1 suspended (`midnight-mart`) |
| Recent platform errors? | 4 sample events (webhook signature failure, SMTP not configured, example-domain suppression, LLM key missing) |
| Forged webhook attempts? | 3 recorded and ignored (`signature_valid = 0`) |

## 5. Safety rules for this data

- **No real person can be contacted.** Every seeded e-mail is on `example.com` or a `*.example` domain; phone numbers are fictional (`+91 90000 NNNNN`). Notification delivery defaults to the **console** provider, seeded deliveries are `SUPPRESSED`/`FAILED`/`QUEUED` (never `SENT`), and the notification service must refuse to send to example domains.
- **GSTINs are synthetic.** They pass the format and checksum rules (`FORMAT_VALID`) but do not belong to real businesses. No seeded business is "officially verified" because no verification service exists.
- **Tax rates are SAMPLE data** (including a 22 Sep 2025 effective-date change used to demonstrate rate versioning). A chartered accountant must verify rates, HSN codes and invoice rules before any real use.
- **Seeding refuses non-local databases**, and `--reset` is the only way to wipe.
- Purchase-invoice "documents" are database rows pointing to `seed://…` paths; no files exist on disk.

## 6. Known simplifications

| Area | Simplification |
|---|---|
| Tax slabs by price (for example apparel under/over ₹2,500) | Not computed; each product has a fixed tax class chosen to match its price |
| Shipping | Taxed as a service line at 18% for registered sellers (SAC 996812); real treatment depends on the supply |
| CGST/SGST | Equal split with the odd paisa on SGST, so totals can differ by a few paise |
| Expiry / batches | Not modelled (groceries and food have no expiry dates) |
| Dates | Orders are generated in IST and stored in UTC; `rpt_*` daily views group by the **UTC** date |
| Refunds | Capped at the captured amount (round-off can make line totals a few paise higher) |
| Reservations | Pending orders and COD orders use a 24-hour expiry so demo data does not go stale |
| Insights tables | `recommendations` and `analysis_runs` are empty: the insights agent computes them |
| Agent history | 40 sample voice logs and 2 conversations only |

## 7. Extending the seed

Add a product: add a `P(...)` to the business in `backend/app/seed/catalogs.py` with a persona; run `make db-reseed`. Add a business: add a `BusinessSpec` to `ALL_BUSINESSES`. Add an invariant: add SQL to `backend/app/seed/verify.py` (it runs after every seed and in tests). Tests: `backend/tests/test_seed.py`.
