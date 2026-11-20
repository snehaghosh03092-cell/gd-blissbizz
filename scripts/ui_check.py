"""Drive the real UI in headless Chrome (DevTools protocol) and fail on any JavaScript error.

Needs: `make dev-all` (any GATEWAY_PORT) and `npm run dev` in frontend/ (port 5173), on a
database you are happy to place a test order in (use blissbiz_smoke). Uses the local Google
Chrome; no extra packages (websockets ships with uvicorn).

Run: backend/.venv/bin/python3 scripts/ui_check.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from websockets.sync.client import connect

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = int(os.environ.get("BLISSBIZ_UI_PORT", "5173"))
SHOP = f"http://threads-co.localhost:{PORT}"
ADMIN = f"http://127.0.0.1:{PORT}"
PASSWORD = "REDACTED-EXAMPLE-CREDENTIAL"
results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    results.append((bool(ok), label))
    print(("PASS  " if ok else "FAIL  ") + label)


class Page:
    def __init__(self, ws_url: str):
        self.ws = connect(ws_url, max_size=50_000_000).__enter__()
        self.n = 0
        self.errors: list[str] = []
        for method in ("Runtime.enable", "Log.enable", "Page.enable", "Network.enable"):
            self.send(method)

    def send(self, method: str, **params):
        self.n += 1
        my = self.n
        self.ws.send(json.dumps({"id": my, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv(timeout=30))
            if msg.get("id") == my:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})
            self._event(msg)

    def _event(self, msg):
        m, p = msg.get("method"), msg.get("params", {})
        if m == "Runtime.exceptionThrown":
            self.errors.append(p["exceptionDetails"].get("exception", {}).get("description", "exception"))
        elif m == "Runtime.consoleAPICalled" and p.get("type") == "error":
            self.errors.append(" ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", [])))
        elif m == "Log.entryAdded" and p["entry"].get("level") == "error":
            text = p["entry"].get("text", "")
            # expected API answers (401 before sign-in, 404 for checks) are not page errors
            if "status of 40" not in text:
                self.errors.append(text)

    def drain(self, seconds: float):
        end = time.time() + seconds
        while time.time() < end:
            try:
                self._event(json.loads(self.ws.recv(timeout=max(0.05, end - time.time()))))
            except TimeoutError:
                break

    def js(self, expression: str):
        r = self.send("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"])
        return r["result"].get("value")

    def go(self, url: str, settle: float = 1.5):
        self.send("Page.navigate", url=url)
        self.drain(settle)
        check(
            self.js("document.documentElement.scrollWidth <= innerWidth + 1"),
            f"no horizontal page overflow at {url}",
        )
        self.send("Emulation.setDeviceMetricsOverride", width=390, height=850, deviceScaleFactor=1, mobile=True)
        check(
            self.js("document.documentElement.scrollWidth <= innerWidth + 1"),
            f"no narrow-screen page overflow at {url}",
        )
        self.send("Emulation.setDeviceMetricsOverride", width=1280, height=900, deviceScaleFactor=1, mobile=False)

    def wait_for(self, expression: str, timeout: float = 10) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.js(f"!!({expression})"):
                return True
            self.drain(0.25)
        return False

    def text(self) -> str:
        return self.js("document.body.innerText") or ""

    def fill(self, selector: str, value: str):
        self.js(f"""(() => {{ const el = document.querySelector({json.dumps(selector)});
            el.value = {json.dumps(value)}; el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}})); }})()""")

    def click_text(self, tag: str, text: str):
        found = self.js(f"""(() => {{ const el = [...document.querySelectorAll({json.dumps(tag)})]
            .find((e) => e.innerText.trim().startsWith({json.dumps(text)}) && !e.disabled);
            if (!el) return false; el.click(); return true; }})()""")
        if not found:
            raise RuntimeError(f"no clickable {tag} '{text}'")
        self.drain(1.0)


def main() -> int:
    profile = tempfile.mkdtemp(prefix="bb-chrome-")
    chrome = subprocess.Popen([CHROME, "--headless=new", "--remote-debugging-port=9333", f"--user-data-dir={profile}",
                               "--no-first-run", "--no-default-browser-check",
                               # BlissBiz's Vite listens on 127.0.0.1; never reach another app on ::1
                               "--host-resolver-rules=MAP *.localhost 127.0.0.1", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
    try:
        for _ in range(50):
            try:
                targets = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json"))
                ws = next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.2)
        page = Page(ws)
        page.send("Emulation.setDeviceMetricsOverride", width=1280, height=900, deviceScaleFactor=1, mobile=False)

        # ---- shopper ------------------------------------------------------------------------
        page.go(SHOP + "/")
        check(page.wait_for("document.querySelector('.card.product')") and "Threads & Co" in page.text(),
              "storefront home renders Threads & Co with product cards")
        page.go(SHOP + "/?q=black&in_stock=true")
        page.wait_for("document.querySelector('.card.product')")
        check("for “black”" in page.text(), "search for 'black' with the in-stock filter shows results")
        slug = page.js("document.querySelector('.card.product').getAttribute('href')")
        page.go(SHOP + slug)
        check(page.wait_for("document.querySelector('.detail h1')"), f"product page {slug} renders")
        page.click_text("button", "Add to cart")
        check(page.wait_for("document.querySelector('.ok')"), "add to cart works for a guest")
        page.go(SHOP + "/cart")
        check(page.wait_for("document.querySelector('tbody tr')"), "cart shows the item")
        page.click_text("a", "Checkout")
        check(page.wait_for("location.pathname === '/login'"), "checkout asks a guest to sign in")
        page.fill("input[type=email]", "aarav.bhatt86@example.com")
        page.fill("input[type=password]", PASSWORD)
        page.click_text("button", "Sign in")
        check(page.wait_for("location.pathname === '/checkout'"), "after signing in the shopper returns to checkout")
        check(page.wait_for("document.querySelector('.grand')", 15), "checkout shows the GST summary and total")
        total = page.js("document.querySelector('.grand').innerText")
        page.click_text("button", "Place order")
        check(page.wait_for("location.pathname.startsWith('/orders/') && document.body.innerText.includes('Thank you')", 15),
              f"order placed from the browser ({total.split(chr(10))[-1] if total else ''})")
        order_path = page.js("location.pathname")
        page.go(SHOP + "/orders")
        check(page.wait_for("document.querySelector('tbody tr')"), "'My orders' lists orders")
        page.go(SHOP + order_path + "/invoice")
        page.go(SHOP + "/register")
        page.go(SHOP + "/policies")

        # ---- owner admin ----------------------------------------------------------------------
        page.go(ADMIN + "/")
        check(page.wait_for("location.pathname === '/login'"), "admin redirects to sign-in")
        page.fill("input[autocomplete=username]", "owner@threads-co.example")
        page.fill("input[type=password]", PASSWORD)
        page.click_text("button", "Sign in")
        check(page.wait_for("location.pathname.startsWith('/b/')", 15), "owner signs in and lands on their business")
        bid = page.js("location.pathname.split('/')[2]")
        check(page.wait_for("document.querySelectorAll('.launch-choice').length === 2"), "owner sees two launch choices")
        page.go(f"{ADMIN}/b/{bid}/overview")
        check(page.wait_for("document.querySelector('.tile')"), "overview tiles render")
        order_id = order_path.split("/")[2]
        page.go(f"{ADMIN}/b/{bid}/orders/{order_id}")
        check(page.wait_for("document.body.innerText.includes('Mark packed')"), "owner opens the new order")
        page.click_text("button", "Mark packed")
        page.wait_for("document.body.innerText.includes('Ship (issues invoice)')")
        page.click_text("button", "Ship (issues invoice)")
        check(page.wait_for("document.body.innerText.match(/TC\\/\\d\\d-\\d\\d\\/\\d{5}/)", 15), "shipping shows the new GST invoice number")
        page.click_text("button", "Mark delivered")
        check(page.wait_for("document.body.innerText.includes('Cash collected on delivery')", 10), "delivery records the cash")
        page.click_text("a", "TC/")
        check(page.wait_for("document.querySelector('.invoice table')") and "Tax invoice" in page.text(), "printable tax invoice renders")
        for path, expect in (("products", "Products"), ("inventory", "Inventory"), ("returns", "Returns"),
                             ("invoices", "Invoices"), ("suppliers", "Suppliers"), ("purchase-orders", "Purchase orders"),
                             ("supplier-invoices", "Supplier invoices"), ("payments", "Supplier payments")):  # fmt: skip
            page.go(f"{ADMIN}/b/{bid}/{path}", settle=2)
            check(page.wait_for(f"document.querySelector('h1') && document.querySelector('h1').innerText.startsWith({json.dumps(expect)})")
                  and not page.js("!!document.querySelector('.error')"), f"admin page '{expect}' renders without errors")
        page.go(f"{ADMIN}/b/{bid}/products", settle=2)
        first = page.js("document.querySelector('tbody a').getAttribute('href')")
        page.go(ADMIN + first, settle=2)
        check(page.wait_for("document.body.innerText.includes('Variants')"), "product editor renders with variants and stock")
        # ---- customer return -> owner refund + credit note ------------------------------------
        page.go(SHOP + order_path, settle=2)
        page.wait_for("document.body.innerText.includes('Return items')")
        page.click_text("button", "Return items")
        page.fill("form.card input[type=number]", "1")
        page.fill("form.card input:not([type=number])", "Did not fit")
        page.click_text("button", "Request return")
        check(page.wait_for("document.body.innerText.includes('requested')"), "customer requests a return from the order page")
        page.go(f"{ADMIN}/b/{bid}/orders/{order_id}", settle=2)
        page.wait_for("document.body.innerText.includes('Approve')")
        page.click_text("button", "Approve")
        page.wait_for("document.body.innerText.includes('Goods received')")
        page.click_text("button", "Goods received")
        page.wait_for("document.body.innerText.includes('Refund & issue credit note')")
        page.click_text("button", "Refund & issue credit note")
        check(page.wait_for("document.body.innerText.includes('Credit note CN/')", 15), "owner approves, receives and refunds; a credit note appears")

        # ---- catalog: create, add a variant, publish --------------------------------------------
        page.go(f"{ADMIN}/b/{bid}/products", settle=2)
        page.click_text("button", "+ New product")
        page.fill("form.card input[required]", "UI Check Kurta")
        page.fill("form.card input[pattern]", "6211")
        page.click_text("button", "Create draft")
        check(page.wait_for("location.pathname.includes('/products/') && document.body.innerText.includes('UI Check Kurta')"), "owner creates a draft product")
        sku = f"UIK-{int(time.time()) % 100000}"
        inputs = "[...document.querySelectorAll('form.card')].find((f) => f.innerText.includes('Add a variant'))"
        page.js(f"""(() => {{ const f = {inputs}; const set = (i, v) => {{ const el = f.querySelectorAll('input')[i]; el.value = v;
            el.dispatchEvent(new Event('input', {{bubbles: true}})); }}; set(0, '{sku}'); set(1, 'size=M, color=Teal'); set(2, '1199'); set(3, '1299'); set(4, '550'); }})()""")
        page.click_text("button", "Add variant")
        page.wait_for(f"document.body.innerText.includes('{sku}')")
        page.click_text("button", "Publish")
        check(page.wait_for("document.querySelector('h1 .badge') && document.querySelector('h1 .badge').innerText.toLowerCase() === 'published'"), f"variant {sku} added and the product published")

        # ---- inventory: count a variant ------------------------------------------------------------
        page.go(f"{ADMIN}/b/{bid}/inventory", settle=2)
        page.wait_for("document.querySelector('tbody tr')")
        page.click_text("button", "Adjust")
        page.fill("form.card input[type=number]", "7")
        page.click_text("button", "Save")
        check(page.wait_for("document.body.innerText.includes('Stock adjusted.')"), "inventory count posts an adjustment")

        # ---- purchasing: supplier, PO, approve, receive ------------------------------------------------
        page.go(f"{ADMIN}/b/{bid}/suppliers", settle=2)
        page.fill("form.card input[required]", f"UI Supplier {sku}")
        page.click_text("button", "Add supplier")
        page.wait_for(f"document.body.innerText.includes('UI Supplier {sku}')")
        page.go(f"{ADMIN}/b/{bid}/purchase-orders", settle=2)
        page.click_text("button", "+ New purchase order")
        page.js(f"""(() => {{ const sel = document.querySelector('form.card select'); const opt = [...sel.options].find((o) => o.text === 'UI Supplier {sku}');
            sel.value = opt.value; sel.dispatchEvent(new Event('change', {{bubbles: true}})); }})()""")
        page.fill(".picker input", sku)
        page.wait_for("document.querySelector('.dropdown button')")
        page.js("document.querySelector('.dropdown button').click()")
        page.drain(0.5)
        page.js("""(() => { const ins = document.querySelectorAll('form.card tbody input'); const set = (el, v) => { el.value = v;
            el.dispatchEvent(new Event('input', {bubbles: true})); }; set(ins[0], '12'); set(ins[1], '560'); })()""")
        page.click_text("button", "Save as draft")
        page.wait_for("document.body.innerText.includes('Approve')")
        page.click_text("button", "Approve")
        page.wait_for("document.body.innerText.includes('Everything outstanding')")
        page.click_text("button", "Everything outstanding")
        page.click_text("button", "Receive goods")
        check(page.wait_for("document.body.innerText.includes('Goods received into stock.')") and "GRN-" in page.text(),
              "purchase order created, approved and received into stock (goods receipt number shown)")

        # ---- supplier bill: upload the original, enter, approve ---------------------------------------------
        page.go(f"{ADMIN}/b/{bid}/supplier-invoices", settle=2)
        pdf = Path(profile) / "bill.pdf"
        pdf.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
        doc = page.send("DOM.getDocument")
        node = page.send("DOM.querySelector", nodeId=doc["root"]["nodeId"], selector="input[type=file]")
        page.send("DOM.setFileInputFiles", files=[str(pdf)], nodeId=node["nodeId"])
        page.click_text("button", "Upload bill")
        page.wait_for("document.body.innerText.includes('Fill in the details')")
        page.js(f"""(() => {{ const f = document.querySelector('form.card'); const sel = f.querySelector('select');
            sel.value = [...sel.options].find((o) => o.text === 'UI Supplier {sku}').value; sel.dispatchEvent(new Event('change', {{bubbles: true}}));
            const ins = f.querySelectorAll('input'); const set = (el, v) => {{ el.value = v; el.dispatchEvent(new Event('input', {{bubbles: true}})); }};
            set(ins[0], 'UI/{sku}'); set(ins[1], '2026-11-12'); set(ins[3], '6720'); set(ins[4], '336'); set(ins[5], '7056'); }})()""")
        page.click_text("button", "Save")
        page.wait_for("document.body.innerText.includes('Saved.')")
        page.click_text("button", "Approve (creates payment due)")
        check(page.wait_for("document.querySelector('form.card .badge') && document.querySelector('form.card .badge').innerText.toLowerCase() === 'approved'"),
              "supplier bill uploaded, filled in and approved")
        page.go(f"{ADMIN}/b/{bid}/payments", settle=2)
        check(page.wait_for("document.body.innerText.includes('7,056.00')"), "the approved bill appears as a payment due")

        # ---- step 13: insights + assistants on the seeded shop --------------------------------------
        page.go(f"{ADMIN}/b/{bid}/insights", settle=3)
        check(page.wait_for("document.querySelectorAll('svg.chart rect.bar').length === 30", 15)
              and page.wait_for("document.body.innerText.includes('Suggested:')", 15),
              "insights page: KPIs, a 30-day revenue chart and recommendations")
        page.fill("form.row input[aria-label=Product]", "classic crew")
        page.click_text("button", "Simulate")
        check(page.wait_for("document.body.innerText.includes('margin falls from')"), "discount what-if answers from the numbers")
        page.go(f"{ADMIN}/b/{bid}/shop-assistant", settle=2)
        check(page.wait_for("document.querySelector('.assistant form input')"), "shop assistant page renders")
        page.go(SHOP + "/", settle=2)
        page.click_text("button", "Ask us")
        check(page.wait_for("document.querySelector('.assistant form input')"), "shopper assistant opens")

        # ---- step 12: a new owner signs up and goes live through the wizard ---------------------------
        page.go(ADMIN + "/", settle=2)
        page.click_text("a", "Sign out")
        page.wait_for("location.pathname === '/login'")
        page.click_text("a", "Start your online shop")
        slug = f"ui-{int(time.time()) % 1000000}"
        username = slug.replace("-", "_")
        page.fill("input[autocomplete=name]", "UI Owner")
        page.fill("input[autocomplete=username]", username)
        page.fill("input[type=email]", f"{slug}@example.com")
        page.fill("input[type=password]", PASSWORD)
        page.fill("input[name=business_name]", "UI Check Boutique")
        page.fill("input[pattern^='[a-z0-9]']", slug)
        page.wait_for("document.body.innerText.includes('is free')")
        page.click_text("button", "Create account")
        check(page.wait_for("document.querySelectorAll('.launch-choice').length === 2", 15), "new owner lands on the launch page")
        page.click_text("a", "Sign out")
        page.wait_for("location.pathname === '/login'")
        page.fill("input[autocomplete=username]", username.upper())
        page.fill("input[type=password]", PASSWORD)
        page.click_text("button", "Sign in")
        check(page.wait_for("document.querySelectorAll('.launch-choice').length === 2"), "new owner can sign in by username")
        page.click_text("a", "Talk to your agent")
        check(page.wait_for("document.querySelector('.guide-drawer .assistant form input')"), "Business Guide opens without submitting a prompt")
        page.js("document.querySelector('.drawer-close').click()")
        page.click_text("a", "Create a website")
        check(page.wait_for("document.querySelectorAll('.template-card').length === 8"), "all eight storefront templates render")
        page.send("Emulation.setDeviceMetricsOverride", width=390, height=850, deviceScaleFactor=1, mobile=True)
        check(page.js("document.documentElement.scrollWidth <= innerWidth + 1"), "setup fits a narrow viewport")
        page.send("Emulation.setDeviceMetricsOverride", width=1280, height=900, deviceScaleFactor=1, mobile=False)
        page.js("document.querySelector('.template-card[value=food]').click()")
        page.click_text("button", "Save template")
        check(page.wait_for("document.querySelector('.template-card[aria-pressed=true]').innerText.includes('Bakery & food')"), "selected template stays selected")
        page.wait_for("document.body.innerText.includes('Still needed')")
        page.js("""(() => { const f = document.querySelector('#business-details'); const sels = f.querySelectorAll('select');
            const pick = (el, v) => { el.value = v; el.dispatchEvent(new Event('change', {bubbles: true})); };
            pick(sels[0], 'clothing'); pick(sels[1], '29'); pick(sels[2], 'ONLINE');
            const city = [...f.querySelectorAll('label')].find((l) => l.innerText.startsWith('City')).querySelector('input');
            city.value = 'Bengaluru'; city.dispatchEvent(new Event('input', {bubbles: true})); })()""")
        page.click_text("button", "Save details")
        page.wait_for("document.body.innerText.includes('Details saved.')")
        page.click_text("button", "Add stock location")
        page.wait_for("document.body.innerText.includes('Stock location added.')")
        page.click_text("button", "Save look")
        page.wait_for("document.body.innerText.includes('Look saved.')")
        page.click_text("button", "Save policies")
        page.wait_for("document.body.innerText.includes('Policies saved.')")
        page.click_text("button", "Go live")
        check(page.wait_for("document.body.innerText.includes('Your shop is live!')", 15), "the wizard saves each step and the shop goes live")
        page.go(f"http://{slug}.localhost:{PORT}/", settle=3)
        check(page.wait_for("document.querySelector('.shop.design-food .storefront-hero')"), "published shop uses the selected bakery template")

        # ---- step 14: platform admin ------------------------------------------------------------------
        page.go(f"http://platform.localhost:{PORT}/", settle=2)
        page.wait_for("location.pathname === '/login'")
        page.fill("input[type=email]", "platform@blissbiz.example")
        page.fill("input[type=password]", PASSWORD)
        page.click_text("button", "Sign in")
        check(page.wait_for(f"document.body.innerText.includes('{slug}.localhost')", 15), "platform admin sees every business, including the new one")
        page.js("window.prompt = () => 'UI check review'; window.confirm = () => true")
        page.js(f"""[...document.querySelectorAll('tbody tr')].find((tr) => tr.innerText.includes('{slug}.localhost'))
            .querySelector('button').click()""")
        page.drain(1.5)
        page.go(f"http://{slug}.localhost:{PORT}/", settle=3)
        check(page.wait_for("document.body.innerText.includes('Shop not found')"), "a suspended business's shop closes")
        page.go(f"http://platform.localhost:{PORT}/", settle=2)
        page.wait_for(f"document.body.innerText.includes('{slug}.localhost')")
        page.js("window.confirm = () => true")
        page.js(f"""[...document.querySelectorAll('tbody tr')].find((tr) => tr.innerText.includes('{slug}.localhost'))
            .querySelector('button').click()""")
        page.drain(1.5)
        page.go(f"http://platform.localhost:{PORT}/audit", settle=2)
        check(page.wait_for("document.body.innerText.includes('business.reinstated')"), "the audit log records suspend and reinstate")
        page.go(f"http://{slug}.localhost:{PORT}/", settle=3)
        check(page.wait_for("document.body.innerText.includes('UI Check Boutique')"), "reinstated shop is open again")

        page.drain(1)
        check(not page.errors, "no JavaScript errors on any page" + (f": {page.errors[:3]}" if page.errors else ""))
    finally:
        chrome.terminate()
        chrome.wait(timeout=10)
        shutil.rmtree(profile, ignore_errors=True)
    failed = [label for ok, label in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} UI checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
