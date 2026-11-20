"""Live checks for steps 4-7 (storefront, customers, cart, COD checkout, order lifecycle), run by
smoke_e2e.py against real services. Every number is compared with SQL or with the seed's own
tax calculator, which is independent of the services' code path."""

import threading
import time
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
from sqlalchemy import text

PASSWORD = "REDACTED-EXAMPLE-CREDENTIAL"
HOST = "threads-co.localhost"


def shopper(base: str, host: str = HOST) -> httpx.Client:
    """A browser on one shop. The Host header picks the shop (what *.localhost does locally)."""
    return httpx.Client(base_url=base, headers={"Host": host, "Origin": f"http://{host}"},
                        timeout=30)  # fmt: skip


def csrf(c: httpx.Client) -> str:
    return c.get("/api/v1/storefront/auth/csrf").json()["csrf_token"]


def run(base: str, owner: httpx.Client, engine, check) -> None:
    def sql(query: str, **p):
        with engine.connect() as conn:
            return conn.execute(text(query), p).all()

    tid = sql("SELECT id FROM businesses WHERE slug='threads-co'")[0][0]
    SF = "/api/v1/storefront"

    # ---- step 4: browsing ------------------------------------------------------------------
    s = shopper(base)
    info = s.get(SF).json()
    check(info["business"]["name"] == "Threads & Co" and info["template_key"] == "fashion",
          "threads-co.localhost serves Threads & Co (fashion template)")  # fmt: skip
    for host, why in (("midnight-mart.localhost", "suspended shop"), ("newleaf.localhost",
                      "shop without a domain"), ("shop.threadsandco.example", "custom domain still VERIFYING"),
                      ("nowhere.localhost", "unknown host")):  # fmt: skip
        r = shopper(base, host).get(f"{SF}/products")
        check(r.status_code == 404 and r.json()["error"]["code"] == "store_not_found",
              f"{host}: 404 store_not_found ({why})")  # fmt: skip
    items, page = [], 1
    while True:
        r = s.get(f"{SF}/products?page={page}&page_size=100").json()
        items += r["items"]
        if len(items) >= r["total"]:
            break
        page += 1
    visible = sql("""SELECT COUNT(*) FROM products p WHERE p.business_id=:b AND p.status='PUBLISHED'
                     AND p.archived_at IS NULL AND EXISTS (SELECT 1 FROM product_variants v WHERE
                     v.product_id=p.id AND v.is_active=1 AND v.archived_at IS NULL)""", b=tid)[0][0]
    check(len(items) == visible, f"shopper sees {len(items)} products = SQL published count")
    hidden = {r[0] for r in sql("SELECT name FROM products WHERE business_id=:b AND status<>'PUBLISHED'",
                                b=tid)}  # fmt: skip
    check(hidden and not hidden & {p["name"] for p in items},
          f"none of the {len(hidden)} draft/archived products is visible")  # fmt: skip
    in_stock_sql = sql("""SELECT COUNT(*) FROM (SELECT p.id FROM products p
        JOIN product_variants v ON v.product_id=p.id AND v.is_active=1 AND v.archived_at IS NULL
        LEFT JOIN inventory_balances b ON b.variant_id=v.id
        LEFT JOIN warehouses w ON w.id=b.warehouse_id AND w.archived_at IS NULL
        WHERE p.business_id=:b AND p.status='PUBLISHED' AND p.archived_at IS NULL
        GROUP BY p.id HAVING SUM(CASE WHEN w.id IS NULL THEN 0
                                      ELSE GREATEST(b.on_hand-b.reserved,0) END) > 0) x""", b=tid)[0][0]
    in_stock = s.get(f"{SF}/products?in_stock=true&page_size=100").json()["total"]
    check(in_stock == in_stock_sql, f"in-stock filter: {in_stock} products = SQL")
    black = s.get(f"{SF}/products?color=black&page_size=100").json()["items"]
    black_sql = {r[0] for r in sql("""SELECT DISTINCT p.id FROM products p JOIN product_variants v
        ON v.product_id=p.id WHERE p.business_id=:b AND p.status='PUBLISHED' AND p.archived_at IS NULL
        AND v.is_active=1 AND v.archived_at IS NULL
        AND LOWER(JSON_UNQUOTE(JSON_EXTRACT(v.options,'$.color')))='black'""", b=tid)}  # fmt: skip
    only_black = all(v["options"].get("color", "").lower() == "black" for p in black for v in p["variants"])
    check({p["id"] for p in black} == black_sql and only_black and black_sql,
          f"'black' filter: {len(black)} products, only black variants, equals SQL")  # fmt: skip
    leak = s.get(f"{SF}/products?page_size=100").text
    check(not any(k in leak for k in ("cost_price", "avg_cost", "reorder_threshold")),
          "no cost or threshold fields reach shoppers")  # fmt: skip

    # ---- step 5: a seeded customer signs in; isolation -----------------------------------------
    email = sql("""SELECT c.email FROM customers c JOIN customer_addresses a ON a.customer_id=c.id
                   WHERE c.business_id=:b AND a.phone IS NOT NULL ORDER BY c.email LIMIT 1""", b=tid)[0][0]
    cid = sql("SELECT id FROM customers WHERE business_id=:b AND email=:e", b=tid, e=email)[0][0]
    # pick a whole-unit variant with plenty of stock in the default warehouse
    vid, vsku, price, tax_class = sql("""SELECT v.id, v.sku, v.selling_price, p.tax_class
        FROM product_variants v JOIN products p ON p.id=v.product_id
        JOIN inventory_balances b ON b.variant_id=v.id JOIN warehouses w ON w.id=b.warehouse_id
        WHERE v.business_id=:b AND p.status='PUBLISHED' AND v.is_active=1 AND p.stock_unit='PCS'
          AND w.is_default=1 AND b.on_hand-b.reserved >= 10 ORDER BY v.sku LIMIT 1""", b=tid)[0]

    def available(variant: str) -> Decimal:
        return sql("""SELECT COALESCE(SUM(b.on_hand-b.reserved),0) FROM inventory_balances b
                      JOIN warehouses w ON w.id=b.warehouse_id AND w.archived_at IS NULL
                      WHERE b.variant_id=:v""", v=variant)[0][0]  # fmt: skip

    token = csrf(s)
    r = s.post(f"{SF}/cart/items", json={"variant_id": vid, "quantity": 2}, headers={"X-CSRF-Token": token})
    check(r.status_code == 200 and "blissbiz_cart" in s.cookies, f"anonymous cart holds 2 x {vsku}")
    r = s.post(f"{SF}/auth/login", json={"email": email, "password": PASSWORD},
               headers={"X-CSRF-Token": token})  # fmt: skip
    check(r.status_code == 200, f"seeded customer {email} signs in")
    token = r.json()["csrf_token"]
    cart = s.get(f"{SF}/cart").json()
    check([(i["sku"], i["quantity"]) for i in cart["items"]] == [(vsku, "2")],
          "anonymous cart merged into the customer's cart")  # fmt: skip
    past = s.get(f"{SF}/orders?page_size=1").json()["total"]
    past_sql = sql("SELECT COUNT(*) FROM orders WHERE business_id=:b AND customer_id=:c", b=tid, c=cid)[0][0]
    check(past == past_sql, f"'my orders' shows their {past} seeded orders")
    fresh = shopper(base, "freshbasket.localhost")
    fresh.cookies.set("blissbiz_session", s.cookies.get("blissbiz_session"))
    check(fresh.get(f"{SF}/auth/me").status_code == 401,
          "the same session cookie is anonymous on another shop")  # fmt: skip
    address = s.get(f"{SF}/me/addresses").json()["items"][0]

    # ---- step 6: checkout --------------------------------------------------------------------
    before = available(vid)
    key = f"smoke-{uuid4()}"
    h = {"X-CSRF-Token": token, "Idempotency-Key": key}
    t0 = time.time()
    r = s.post(f"{SF}/checkout", json={"address_id": address["id"]}, headers=h)
    ms = int((time.time() - t0) * 1000)
    order = r.json()
    check(r.status_code == 201 and order["order_status"] == "CONFIRMED" and order["payment_method"] == "COD",
          f"COD checkout placed {order.get('order_number')} in {ms} ms")  # fmt: skip
    check(available(vid) == before - 2, f"2 units reserved ({before:g} -> {available(vid):g} available)")
    again = s.post(f"{SF}/checkout", json={"address_id": address["id"]}, headers=h)
    check(again.status_code == 200 and again.json()["id"] == order["id"] and available(vid) == before - 2,
          "same Idempotency-Key returns the same order, nothing reserved twice")  # fmt: skip
    # independent re-computation with the seed's calculator
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    from app.seed.taxes import quote_order

    seller_state, registered, inclusive = sql("""SELECT b.state_code, t.gst_registered,
        t.prices_include_tax FROM businesses b JOIN business_tax_profiles t ON t.business_id=b.id
        WHERE b.id=:b""", b=tid)[0]  # fmt: skip
    free_over, flat = sql("SELECT free_shipping_over, flat_shipping_fee FROM store_policies WHERE business_id=:b",
                          b=tid)[0]  # fmt: skip
    goods = price * 2
    fee = Decimal(0) if free_over is not None and goods >= free_over else (flat or Decimal(0))
    from app.db.models import utc_now

    expect = quote_order([(Decimal(2), price, tax_class)], seller_registered=bool(registered),
                         inter_state=address["state_code"] != seller_state, include_tax=bool(inclusive),
                         discount_pct=Decimal(0), shipping_gross=fee,
                         on=(utc_now() + timedelta(hours=5, minutes=30)).date())  # fmt: skip
    check((Decimal(order["grand_total"]), Decimal(order["tax_total"]), Decimal(order["shipping_total"]))
          == (expect.grand_total, expect.tax_total, expect.shipping_total),
          f"totals match the independent tax calculator (Rs {order['grand_total']}, GST {order['tax_total']})")  # fmt: skip

    # ---- step 7: owner fulfils -----------------------------------------------------------------
    oc = owner.get("/api/v1/auth/csrf").json()["csrf_token"]
    oh = {"X-CSRF-Token": oc}
    obase = f"/api/v1/admin/businesses/{tid}/orders"
    found = owner.get(f"{obase}?q={order['order_number']}").json()["items"]
    check([o["id"] for o in found] == [order["id"]], "owner finds the order by number")
    on_hand_before = sql("SELECT SUM(on_hand) FROM inventory_balances WHERE variant_id=:v", v=vid)[0][0]
    check(owner.post(f"{obase}/{order['id']}/pack", headers=oh).json()["order_status"] == "PACKED", "owner packs")
    shipped = owner.post(f"{obase}/{order['id']}/ship", json={"carrier": "Delhivery"}, headers=oh).json()
    sale = sql("""SELECT COALESCE(SUM(quantity_delta),0), COUNT(*) FROM inventory_transactions
                  WHERE source_type='ORDER' AND source_id=:o AND txn_type='SALE'""", o=order["id"])[0]  # fmt: skip
    on_hand_after = sql("SELECT SUM(on_hand) FROM inventory_balances WHERE variant_id=:v", v=vid)[0][0]
    check(shipped["order_status"] == "SHIPPED" and sale[0] == -2 and on_hand_after == on_hand_before - 2,
          "ship writes a SALE of -2 to the ledger and on-hand drops by 2")  # fmt: skip
    owner.post(f"{obase}/{order['id']}/ship", json={}, headers=oh)
    check(sql("SELECT COUNT(*) FROM inventory_transactions WHERE source_id=:o", o=order["id"])[0][0] == sale[1],
          "shipping twice does not sell twice")  # fmt: skip
    done = owner.post(f"{obase}/{order['id']}/deliver", headers=oh).json()
    pay = sql("SELECT provider, status, amount FROM payments WHERE order_id=:o", o=order["id"])
    check(done["payment_status"] == "PAID" and pay == [("cod", "CAPTURED", Decimal(order["grand_total"]))],
          "deliver records the cash collected (payment CAPTURED, order PAID)")  # fmt: skip

    # customer cancels a second order: stock comes back
    s.post(f"{SF}/cart/items", json={"variant_id": vid, "quantity": 1}, headers={"X-CSRF-Token": token})
    second = s.post(f"{SF}/checkout", json={"address_id": address["id"]},
                    headers={"X-CSRF-Token": token, "Idempotency-Key": f"smoke-{uuid4()}"}).json()  # fmt: skip
    mid = available(vid)
    cancelled = s.post(f"{SF}/orders/{second['id']}/cancel", json={}, headers={"X-CSRF-Token": token}).json()
    check(cancelled["order_status"] == "CANCELLED" and available(vid) == mid + 1,
          "customer cancels before packing; the held unit is released")  # fmt: skip
    check(fresh.get(f"{SF}/orders/{order['id']}").status_code in (401, 404),
          "another shop cannot see this order")  # fmt: skip

    # ---- concurrency: N shoppers race for the last units ---------------------------------------
    row = sql("""SELECT v.id, v.sku, SUM(b.on_hand-b.reserved) AS free FROM product_variants v
        JOIN products p ON p.id=v.product_id JOIN inventory_balances b ON b.variant_id=v.id
        JOIN warehouses w ON w.id=b.warehouse_id AND w.archived_at IS NULL
        WHERE v.business_id=:b AND p.status='PUBLISHED' AND v.is_active=1 AND p.stock_unit='PCS'
        GROUP BY v.id, v.sku HAVING free BETWEEN 2 AND 4 ORDER BY v.sku LIMIT 1""", b=tid)[0]  # fmt: skip
    last, last_sku, units = row[0], row[1], int(row[2])
    racers = []
    for n in range(units + 4):
        c = shopper(base)
        t = csrf(c)
        reg = c.post(f"{SF}/auth/register", headers={"X-CSRF-Token": t}, json={
            "email": f"racer{n}.{uuid4().hex[:8]}@example.com", "password": PASSWORD,
            "full_name": f"Racer {n}"})  # fmt: skip
        t = reg.json()["csrf_token"]
        a = c.post(f"{SF}/me/addresses", headers={"X-CSRF-Token": t}, json={
            "recipient": f"Racer {n}", "phone": "9876543210", "line1": "1 Test Street",
            "city": "Bengaluru", "state_code": "29", "postal_code": "560001"}).json()  # fmt: skip
        c.post(f"{SF}/cart/items", json={"variant_id": last, "quantity": 1}, headers={"X-CSRF-Token": t})
        racers.append((c, t, a["id"]))
    results: list[int] = []
    gate = threading.Barrier(len(racers))

    def race(c, t, address_id):
        gate.wait()
        r = c.post(f"{SF}/checkout", json={"address_id": address_id},
                   headers={"X-CSRF-Token": t, "Idempotency-Key": f"race-{uuid4()}"})  # fmt: skip
        results.append(r.status_code)

    threads = [threading.Thread(target=race, args=r) for r in racers]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    wins, losses = results.count(201), results.count(409)
    negative = sql("SELECT COUNT(*) FROM inventory_balances WHERE reserved > on_hand OR on_hand < 0")[0][0]
    check(wins == units and losses == len(racers) - units and available(last) == 0 and negative == 0,
          f"{len(racers)} shoppers race for the last {units} x {last_sku}: {wins} orders, "
          f"{losses} told 'out of stock', 0 oversold")  # fmt: skip
