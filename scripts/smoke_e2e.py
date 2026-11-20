"""Start all seven real services on free ports and drive the real flows over HTTP, cross-checking
every number against independent SQL.

It never touches your `blissbiz` database: it migrates and seeds its own local `blissbiz_smoke`
database first (same seed), because checkout writes orders and append-only ledger rows.

Run: make smoke   (needs MySQL up and `make smoke-db` once; exits non-zero on any failure)
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / "backend" / ".venv" / "bin" / "python3")
PASSWORD = "REDACTED-EXAMPLE-CREDENTIAL"
ORIGIN = "http://127.0.0.1:5173"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


LOGS = ROOT / "var" / "smoke-logs"


def spawn(module: str, port: int, env: dict) -> subprocess.Popen:
    """Each service logs to var/smoke-logs/<module>.log (a pipe nobody reads would fill up and
    freeze the service once ~64 KB of logs accumulate)."""
    LOGS.mkdir(parents=True, exist_ok=True)
    log = open(LOGS / f"{module.split('.')[0]}.log", "w")  # noqa: SIM115 (closed with the process)
    return subprocess.Popen(
        [PY, "-m", "uvicorn", f"{module}:app", "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )  # fmt: skip


def wait(url: str) -> None:
    for _ in range(60):
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise SystemExit(f"timed out waiting for {url}")


results: list[tuple[bool, str]] = []
ENGINE = None  # this script's own SQL connection: always the smoke database, set in main()


def check(ok: bool, label: str) -> None:
    results.append((ok, label))
    print(("PASS  " if ok else "FAIL  ") + label)


def session(base: str) -> httpx.Client:
    return httpx.Client(base_url=base, headers={"Origin": ORIGIN})


def login(client: httpx.Client, email: str, password: str = PASSWORD) -> httpx.Response:
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    return client.post("/api/v1/auth/login", json={"email": email, "password": password},
                       headers={"X-CSRF-Token": csrf})  # fmt: skip


def catalog_checks(
    base: str, tid: str, fid: str, c: httpx.Client, other: httpx.Client
) -> None:
    def sql(query: str, **p):
        with ENGINE.connect() as conn:
            return conn.execute(text(query), {"b": tid, **p}).all()

    biz = f"/api/v1/admin/businesses/{tid}"
    products = c.get(f"{biz}/products?page_size=100").json()
    expected = sql("SELECT COUNT(*) FROM products WHERE business_id=:b")[0][0]
    check(
        products["total"] == expected,
        f"product total {products['total']} equals SQL count {expected}",
    )
    pub = c.get(f"{biz}/products?status=PUBLISHED&page_size=100").json()["total"]
    published_sql = sql(
        "SELECT COUNT(*) FROM products WHERE business_id=:b AND status='PUBLISHED'"
    )[0][0]
    check(pub == published_sql, f"published products ({pub}) equal SQL")
    cats = {
        x["slug"]: x["products"] for x in c.get(f"{biz}/categories").json()["items"]
    }
    sql_cats = dict(
        sql(
            "SELECT c.slug, COUNT(p.id) FROM categories c LEFT JOIN products p ON p.category_id=c.id "
            "AND p.status<>'ARCHIVED' WHERE c.business_id=:b GROUP BY c.slug"
        )
    )
    check(
        cats == {k: int(v) for k, v in sql_cats.items()},
        f"category counts match SQL ({len(cats)})",
    )
    t0 = time.time()
    inv = c.get(f"{biz}/inventory?page_size=100").json()
    ms = int((time.time() - t0) * 1000)
    low_sql = sql("SELECT COUNT(*) FROM rpt_low_stock WHERE business_id=:b")[0][0]
    out_sql = sql(
        "SELECT COUNT(*) FROM rpt_inventory_current WHERE business_id=:b AND product_status='PUBLISHED' "
        "AND is_active=1 AND available<=0"
    )[0][0]
    all_sql = sql("SELECT COUNT(*) FROM rpt_inventory_current WHERE business_id=:b")[0][
        0
    ]
    summary = inv["summary"]
    check(
        summary["low_stock"] == low_sql,
        f"low-stock count {summary['low_stock']} equals rpt_low_stock",
    )
    check(
        summary["out_of_stock"] == out_sql,
        f"out-of-stock count {summary['out_of_stock']} equals SQL",
    )
    check(
        inv["total"] == summary["variants"] == all_sql,
        f"all {all_sql} variants joined in {ms} ms",
    )
    low_items = c.get(f"{biz}/inventory?low_only=true&page_size=100").json()["items"]
    low_skus = {r[0] for r in sql("SELECT sku FROM rpt_low_stock WHERE business_id=:b")}
    check(
        {i["sku"] for i in low_items} == low_skus,
        "low-stock SKU list identical to the read view",
    )
    tee = next(p for p in products["items"] if p["name"] == "Classic Crew T-Shirt")
    detail = c.get(f"{biz}/products/{tee['id']}").json()
    api_total = sum(float(v["stock"]["available"]) for v in detail["variants"])
    sql_total = float(
        sql(
            "SELECT SUM(available) FROM rpt_inventory_current WHERE business_id=:b AND product_id=:p",
            p=tee["id"],
        )[0][0]
    )
    check(
        api_total == sql_total,
        f"'Classic Crew T-Shirt' stock {api_total:g} equals SQL ({len(detail['variants'])} variants)",
    )
    check(all("cost_price" in v for v in detail["variants"]), "owner sees cost prices")
    v0 = detail["variants"][0]
    rows, page = [], 1
    while True:
        r = c.get(
            f"{biz}/inventory/ledger?variant_id={v0['id']}&page={page}&page_size=100"
        ).json()
        rows += r["items"]
        if len(rows) >= r["total"]:
            break
        page += 1
    ledger_sum = sum(float(x["quantity"]) for x in rows)
    check(
        ledger_sum == float(v0["stock"]["on_hand"]),
        f"ledger of {v0['sku']} ({len(rows)} entries) sums to on-hand",
    )
    staff = session(base)
    login(staff, "staff@threads-co.example")
    sd = staff.get(f"{biz}/products/{tee['id']}").json()
    check(
        all("cost_price" not in v for v in sd["variants"]),
        "staff cannot see cost prices",
    )
    check(
        staff.get(f"{biz}/inventory?low_only=true").status_code == 200,
        "staff can read inventory",
    )
    check(
        other.get(f"{biz}/products").status_code == 404,
        "another business's owner: 404 on products",
    )
    check(other.get(f"{biz}/inventory").status_code == 404, "...and on inventory")
    cross = other.get(f"/api/v1/admin/businesses/{fid}/products/{tee['id']}")
    check(
        cross.status_code == 404,
        "own path + Threads & Co product id -> 404 (no cross-tenant read)",
    )
    own = other.get(f"/api/v1/admin/businesses/{fid}/inventory?page_size=100").json()
    mixed = any(i["sku"].startswith("THR-") for i in own["items"])
    check(
        own["total"] > 0 and not mixed,
        f"FreshBasket sees only its own {own['total']} variants",
    )


SERVICES = ["identity", "store", "catalog", "supply", "sales", "billing", "agent"]


def smoke_database_url() -> str:
    """The configured DATABASE_URL with the database renamed to blissbiz_smoke (same server)."""
    sys.path.insert(0, str(ROOT / "backend"))
    from app.core.config import settings
    from sqlalchemy.engine import make_url

    url = make_url(settings.database_url.get_secret_value())
    if url.get_backend_name() != "mysql" or url.host not in ("127.0.0.1", "localhost"):
        raise SystemExit("smoke needs the local MySQL from compose.yaml")
    return url.set(database="blissbiz_smoke").render_as_string(hide_password=False)


def prepare_database(env: dict) -> None:
    for args in (["-m", "alembic", "upgrade", "head"], ["-m", "app.seed", "--reset"]):
        done = subprocess.run([PY, *args], cwd=ROOT / "backend", env=env, capture_output=True, text=True)
        if done.returncode != 0:
            raise SystemExit(f"preparing blissbiz_smoke failed ({' '.join(args)}):\n{done.stderr[-2000:]}")


def main() -> int:
    global ENGINE
    from sqlalchemy import create_engine

    database_url = smoke_database_url()
    ENGINE = create_engine(database_url, pool_pre_ping=True)
    if ENGINE.url.database != "blissbiz_smoke":
        raise SystemExit("refusing to run: the smoke checks must use blissbiz_smoke")
    ports = {name: free_port() for name in SERVICES}
    gw_port = free_port()
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "INTERNAL_SIGNING_SECRET": "REDACTED-EXAMPLE-CREDENTIAL",
        **{f"{name.upper()}_URL": f"http://127.0.0.1:{port}" for name, port in ports.items()},
    }
    print("preparing blissbiz_smoke (migrate + seed) ...")
    prepare_database(env)
    procs = [spawn(f"{name}_service.main", port, env) for name, port in ports.items()]
    procs.append(spawn("gateway_service.main", gw_port, env))
    id_port = ports["identity"]
    try:
        for port in ports.values():
            wait(f"http://127.0.0.1:{port}/health")
        wait(f"http://127.0.0.1:{gw_port}/api/ready")
        base = f"http://127.0.0.1:{gw_port}"
        print(f"gateway on {base}; " + ", ".join(f"{n} :{p}" for n, p in ports.items()) + "\n")

        c = session(base)
        check(
            c.get("/api/ready").json() == {"status": "ready"},
            "gateway ready (all 7 services reachable)",
        )
        bad = login(session(base), "owner@threads-co.example", "wrong-password-123456")
        check(
            bad.status_code == 401
            and bad.json()["error"]["code"] == "invalid_credentials",
            "wrong password -> 401",
        )

        r = login(c, "owner@threads-co.example")
        check(
            r.status_code == 200,
            "owner of Threads & Co logs in with seeded credentials",
        )
        me = c.get("/api/v1/auth/me").json()
        check(
            me["owner"]["email"] == "owner@threads-co.example",
            "GET /auth/me returns the owner",
        )
        biz = {b["slug"]: b for b in c.get("/api/v1/admin/businesses").json()["items"]}
        check(
            list(biz) == ["threads-co"] and biz["threads-co"]["role"] == "OWNER",
            "sees only their business, as OWNER",
        )
        detail = c.get(f"/api/v1/admin/businesses/{biz['threads-co']['id']}").json()
        check(
            "team.manage" in detail["permissions"],
            "owner permissions include team.manage",
        )

        staff = session(base)
        check(
            login(staff, "staff@threads-co.example").status_code == 200,
            "staff member logs in",
        )
        sd = staff.get(f"/api/v1/admin/businesses/{biz['threads-co']['id']}").json()
        check(
            sd["role"] == "STAFF" and "catalog.write" not in sd["permissions"],
            "staff get staff-only permissions",
        )

        other = session(base)
        login(other, "owner@freshbasket.example")
        cross = other.get(f"/api/v1/admin/businesses/{biz['threads-co']['id']}")
        check(
            cross.status_code == 404,
            "another business's owner gets 404 for Threads & Co",
        )
        fresh_id = other.get("/api/v1/admin/businesses").json()["items"][0]["id"]
        catalog_checks(base, biz["threads-co"]["id"], fresh_id, c, other)

        print("\n-- steps 4-7: storefront, customers, cart, checkout, orders --")
        import smoke_shop

        smoke_shop.run(base, c, ENGINE, check)

        print("\n-- steps 8-11 backend: invoices, returns, refunds, expiry, editing, stock, purchasing --")
        import smoke_ops

        smoke_ops.run(base, c, ENGINE, check, f"http://127.0.0.1:{ports['sales']}", env["INTERNAL_SIGNING_SECRET"])

        print("\n-- steps 12-14: sign-up to live shop, assistants, insights, platform admin --")
        import smoke_journey

        smoke_journey.run(base, ENGINE, check)

        csrf = c.get("/api/v1/auth/csrf").json()["csrf_token"]
        check(
            c.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf}).status_code
            == 204,
            "logout",
        )
        check(
            c.get("/api/v1/auth/me").status_code == 401, "session is dead after logout"
        )

        direct = httpx.post(
            f"http://127.0.0.1:{id_port}/internal/v1/sessions/resolve",
            json={"raw_token": "x"},
        )
        check(direct.status_code == 401, "identity rejects a direct unsigned call")
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(timeout=10)
    print("\n-- integrity checks on blissbiz_smoke after all of that --")
    from app.seed.verify import run_checks

    broken = {name: n for name, n in run_checks(ENGINE).items() if n}
    check(not broken, f"all integrity checks pass after live orders {broken or ''}")
    failed = [label for ok, label in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
