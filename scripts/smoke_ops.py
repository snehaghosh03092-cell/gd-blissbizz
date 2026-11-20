"""Live checks for invoices, returns/refunds, paid cancellation, reservation expiry, catalog
editing, stock operations and purchasing (steps 8-11 backend), on the seeded smoke database.
Numbers are compared with SQL; the integrity checks run afterwards in smoke_e2e.py."""

import base64
import re
from decimal import Decimal
from uuid import uuid4

import httpx
from sqlalchemy import text

from smoke_shop import PASSWORD, csrf, shopper

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def run(base: str, owner: httpx.Client, engine, check, sales_url: str, secret: str) -> None:
    def sql(query: str, **p):
        with engine.connect() as conn:
            return conn.execute(text(query), p).all()

    def write(query: str, **p):
        with engine.begin() as conn:
            conn.execute(text(query), p)

    tid = sql("SELECT id FROM businesses WHERE slug='threads-co'")[0][0]
    adm = f"/api/v1/admin/businesses/{tid}"
    oc = owner.get("/api/v1/auth/csrf").json()["csrf_token"]
    H = {"X-CSRF-Token": oc}

    def post(path, body=None):
        return owner.post(adm + path, json=body or {}, headers=H)

    def available(variant: str) -> Decimal:
        return sql("""SELECT COALESCE(SUM(b.on_hand-b.reserved),0) FROM inventory_balances b
                      JOIN warehouses w ON w.id=b.warehouse_id AND w.archived_at IS NULL
                      WHERE b.variant_id=:v""", v=variant)[0][0]

    # ---- invoice at dispatch, continuing the seeded gapless numbering -------------------------
    s = shopper(base)
    email = sql("""SELECT c.email FROM customers c JOIN customer_addresses a ON a.customer_id=c.id
                   WHERE c.business_id=:b AND a.phone IS NOT NULL ORDER BY c.email LIMIT 1 OFFSET 3""", b=tid)[0][0]
    t = csrf(s)
    t = s.post("/api/v1/storefront/auth/login", json={"email": email, "password": PASSWORD},
               headers={"X-CSRF-Token": t}).json()["csrf_token"]
    vid = sql("""SELECT v.id FROM product_variants v JOIN products p ON p.id=v.product_id
        JOIN inventory_balances b ON b.variant_id=v.id JOIN warehouses w ON w.id=b.warehouse_id
        WHERE v.business_id=:b AND p.status='PUBLISHED' AND v.is_active=1 AND p.stock_unit='PCS'
          AND w.is_default=1 AND b.on_hand-b.reserved >= 10 ORDER BY v.sku DESC LIMIT 1""", b=tid)[0][0]
    address = s.get("/api/v1/storefront/me/addresses").json()["items"][0]["id"]
    s.post("/api/v1/storefront/cart/items", json={"variant_id": vid, "quantity": 3}, headers={"X-CSRF-Token": t})
    order = s.post("/api/v1/storefront/checkout", json={"address_id": address},
                   headers={"X-CSRF-Token": t, "Idempotency-Key": f"ops-{uuid4()}"}).json()
    fy_rows = sql("""SELECT fy, last_number FROM invoice_sequences WHERE business_id=:b AND series='TC'
                     ORDER BY fy DESC LIMIT 1""", b=tid)
    before = fy_rows[0][1] if fy_rows else 0
    post(f"/orders/{order['id']}/pack")
    shipped = post(f"/orders/{order['id']}/ship", {"carrier": "Blue Dart", "tracking_number": "BD1"}).json()
    number = shipped["invoice"]["number"]
    check(re.fullmatch(r"TC/\d\d-\d\d/\d{5}", number) and int(number[-5:]) == before + 1,
          f"dispatch issued invoice {number}, the next gapless number after the seeded {before}")
    inv = owner.get(f"{adm}/orders/{order['id']}/invoice").json()
    lines_sum = sum(Decimal(x["line_total"]) for x in inv["lines"]) + Decimal(inv["round_off"])
    check(Decimal(inv["grand_total"]) == Decimal(order["grand_total"]) == lines_sum,
          f"invoice total {inv['grand_total']} = order total = sum of its lines")
    check(s.get(f"/api/v1/storefront/orders/{order['id']}/invoice").json()["number"] == number,
          "the customer can open their own invoice")
    post(f"/orders/{order['id']}/deliver")

    # ---- return 1 of 3, credit note, refund ---------------------------------------------------
    item = order["items"][0]["id"]
    free_before = available(vid)
    r = s.post(f"/api/v1/storefront/orders/{order['id']}/returns", headers={"X-CSRF-Token": t},
               json={"reason": "Wrong size", "items": [{"order_item_id": item, "quantity": 1}]})
    rid = r.json()["returns"][0]["id"]
    for action, body in (("approve", {}), ("receive", {}), ("refund", {"method": "UPI", "reference": "UPI-1"})):
        done = post(f"/returns/{rid}/{action}", body)
    done = done.json()
    note = done["invoice"]["credit_notes"][0]
    refund = Decimal(done["refunds"][0]["amount"])
    third = (Decimal(order["items"][0]["line_total"]) / 3).quantize(Decimal("1"))
    check(done["payment_status"] == "PARTIALLY_REFUNDED" and refund == Decimal(note["grand_total"]) and abs(refund - third) <= 1,
          f"return of 1 of 3: credit note {note['number']} and refund Rs {refund} (a third of the line)")
    check(available(vid) == free_before + 1, "the returned unit is back in stock")

    # ---- cancel a seeded, paid online order: restock and full refund ----------------------------
    paid = sql("""SELECT o.id, o.grand_total FROM orders o JOIN payments p ON p.order_id=o.id AND p.status='CAPTURED'
                  WHERE o.business_id=:b AND o.payment_method='ONLINE' AND o.order_status IN ('CONFIRMED','PACKED')
                  ORDER BY o.placed_at DESC LIMIT 1""", b=tid)
    if paid:
        oid, grand = paid[0]
        c = post(f"/orders/{oid}/cancel", {"reason": "Customer asked"}).json()
        back = sql("""SELECT COUNT(*) FROM inventory_transactions WHERE source_id=:o AND reason_code='ORDER_CANCELLED'""", o=oid)[0][0]
        check(c.get("order_status") == "CANCELLED" and c.get("payment_status") == "REFUNDED"
              and Decimal(c["refunds"][0]["amount"]) == grand and back > 0,
              f"cancelling a paid online order refunds Rs {grand} and restocks {back} line(s)")
    else:
        check(True, "no paid-but-unshipped online order in this seed (cancellation path covered by tests)")

    # ---- reservation expiry sweep (signed call, as the background loop does) ---------------------
    write("UPDATE stock_reservations SET expires_at = UTC_TIMESTAMP() - INTERVAL 1 MINUTE WHERE status='ACTIVE' AND business_id=:b", b=tid)
    confirmed = sql("""SELECT COUNT(DISTINCT r.order_id) FROM stock_reservations r JOIN orders o ON o.id=r.order_id
                       WHERE r.status='ACTIVE' AND r.business_id=:b AND o.order_status IN ('CONFIRMED','PENDING_PAYMENT')""", b=tid)[0][0]
    packed = sql("""SELECT COUNT(DISTINCT r.order_id) FROM stock_reservations r JOIN orders o ON o.id=r.order_id
                    WHERE r.status='ACTIVE' AND r.business_id=:b AND o.order_status='PACKED'""", b=tid)[0][0]
    result = signed_post(sales_url, "/internal/v1/jobs/expire-reservations", secret)
    still_packed = sql("""SELECT COUNT(DISTINCT r.order_id) FROM stock_reservations r JOIN orders o ON o.id=r.order_id
                          WHERE r.status='ACTIVE' AND r.business_id=:b AND o.order_status='PACKED'
                          AND r.expires_at > UTC_TIMESTAMP()""", b=tid)[0][0]
    check(result["cancelled"] >= confirmed and result["extended"] >= packed and still_packed == packed and result["failed"] == 0,
          f"expiry sweep: {result['cancelled']} unpacked orders cancelled, {result['extended']} packed holds extended, 0 failures")

    # ---- catalog editing + opening stock -------------------------------------------------------------
    cat = post("/categories", {"name": f"Smoke {uuid4().hex[:6]}"}).json()
    p = post("/products", {"name": "Smoke Linen Shirt", "category_id": cat["id"], "tax_class": "GST_5", "hsn_sac": "6205"}).json()
    sku = f"SMK-{uuid4().hex[:6].upper()}"
    v = post(f"/products/{p['id']}/variants", {"sku": sku, "options": {"size": "L", "color": "Sand"},
                                               "selling_price": "1299", "mrp": "1499", "cost_price": "610"}).json()["variants"][0]
    post(f"/products/{p['id']}/publish")
    main_wh = sql("SELECT id FROM warehouses WHERE business_id=:b AND is_default=1", b=tid)[0][0]
    other_wh = sql("SELECT id FROM warehouses WHERE business_id=:b AND is_default=0 AND archived_at IS NULL LIMIT 1", b=tid)[0][0]
    post("/inventory/opening", {"warehouse_id": main_wh, "lines": [{"variant_id": v["id"], "quantity": "20"}]})
    seen = shopper(base).get(f"/api/v1/storefront/products/{p['slug']}").json()
    check(seen.get("availability") == "IN_STOCK", f"a new product ({sku}) is published, stocked and visible to shoppers")

    # ---- stock operations ------------------------------------------------------------------------------
    adj = post("/inventory/adjustments", {"id": str(uuid4()), "reason_code": "CYCLE_COUNT",
                                          "lines": [{"variant_id": v["id"], "warehouse_id": main_wh, "counted_qty": "18"}]}).json()
    t1 = post("/inventory/transfers", {"from_warehouse_id": main_wh, "to_warehouse_id": other_wh,
                                       "lines": [{"variant_id": v["id"], "quantity": "5"}]}).json()
    post(f"/inventory/transfers/{t1['id']}/receive")
    split = dict(sql("SELECT warehouse_id, on_hand FROM inventory_balances WHERE variant_id=:v", v=v["id"]))
    check(adj["lines"][0]["delta"] == "-2" and split == {main_wh: 13, other_wh: 5},
          "count 18 of 20 posts -2; transfer of 5 lands in the second warehouse (13 + 5)")
    rec = post("/inventory/reconcile").json()
    check(rec["discrepancies"] == [], f"reconciliation over {rec['checked']} balances finds no differences")

    # ---- purchasing ---------------------------------------------------------------------------------------
    sup = post("/suppliers", {"name": f"Smoke Supplier {uuid4().hex[:6]}", "payment_terms_days": 15}).json()
    po = post("/purchase-orders", {"supplier_id": sup["id"], "lines": [{"variant_id": v["id"], "quantity": "10", "unit_cost": "650"}]}).json()
    post(f"/purchase-orders/{po['id']}/approve")
    key = f"grn-{uuid4()}"
    body = {"idempotency_key": key, "warehouse_id": main_wh, "lines": [{"po_item_id": po["items"][0]["id"], "quantity": "10"}]}
    got = post(f"/purchase-orders/{po['id']}/receipts", body).json()
    again = post(f"/purchase-orders/{po['id']}/receipts", body).json()
    on_hand, avg = sql("SELECT on_hand, avg_cost FROM inventory_balances WHERE variant_id=:v AND warehouse_id=:w", v=v["id"], w=main_wh)[0]
    expected_avg = ((Decimal(13) * Decimal("610") + Decimal(10) * Decimal("650")) / 23).quantize(Decimal("0.0001"))
    check(got["purchase_order"]["status"] == "RECEIVED" and again["replayed"] and on_hand == 23 and avg == expected_avg,
          f"PO received once (retry replayed): 23 on hand, average cost {avg} = SQL-independent {expected_avg}")
    doc = post("/documents", {"filename": "bill.pdf", "content_base64": base64.b64encode(PDF).decode()}).json()
    pi = post("/purchase-invoices", {"document_id": doc["id"], "supplier_id": sup["id"], "supplier_invoice_number": f"SB-{uuid4().hex[:5]}",
                                     "invoice_date": "2026-11-10", "taxable_total": "6500.00", "tax_total": "325.00",
                                     "grand_total": "6825.00", "linked_po_id": po["id"]}).json()
    approved = post(f"/purchase-invoices/{pi['id']}/approve").json()
    due = [x for x in owner.get(f"{adm}/supplier-payments").json()["items"] if x["purchase_invoice_id"] == pi["id"]]
    paid_now = owner.post(f"{adm}/supplier-payments/{due[0]['id']}/pay", json={"amount": "6825.00"}, headers=H).json()
    raw = owner.get(f"{adm}/documents/{doc['id']}/content").content
    check(approved["status"] == "APPROVED" and due[0]["due_date"] == "2026-11-25" and paid_now["status"] == "PAID" and raw == PDF,
          "supplier bill approved -> payable due in 15 days -> paid; original file returned byte for byte")


def signed_post(base_url: str, path: str, secret: str) -> dict:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libs" / "blissbiz_common"))
    from blissbiz_common.context import InternalContext
    from blissbiz_common.signing import sign_request

    ctx = InternalContext(request_id=f"smoke-{uuid4().hex[:12]}", actor_type="SERVICE", actor_id="scheduler")
    headers = sign_request(secret, "POST", path, b"", ctx)
    r = httpx.post(base_url + path, headers=headers, timeout=60)
    r.raise_for_status()
    return r.json()

