"""Document actions, real XLSX/ZIP content, no writes, and tenant/role boundaries."""

import json
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from agent_service.documents import intent, requested_period
from app.db.models.billing import CreditNote, Invoice
from blissbiz_common.errors import ApiError
from blissbiz_common.workbooks import workbook
from conftest import ORIGIN, login, shop, shopper_login
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_invoices_returns import act, place

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def sheet(content, number):
    with ZipFile(BytesIO(content)) as archive:
        assert archive.testzip() is None
        root = ET.fromstring(archive.read(f"xl/worksheets/sheet{number}.xml"))
        return [
            ["".join(cell.itertext()) for cell in row]
            for row in root.findall("s:sheetData/s:row", NS)
        ]


@pytest.fixture
def owner(world):
    c = world["client"]
    c.csrf = login(c).json()["csrf_token"]
    return c


def ask(world, owner, question, conversation=None, business="acme"):
    return owner.post(
        f"/api/v1/admin/businesses/{world['ids'][business]}/business-guide",
        json={"text": question, "conversation_id": conversation},
        headers={"Origin": ORIGIN, "X-CSRF-Token": owner.csrf},
    )


def test_exact_requests_create_download_actions_without_provider(world, owner):
    questions = [
        (
            "can you prepare for me the documents that I needed for GST registration",
            "registration_pack",
        ),
        ("can you collect all the invoices that is needed for GST", "invoice_export"),
        ("I need you to collect the invoices for the orders I", "invoice_export"),
        ("Prepare my GST and shop registration documents", "registration_pack"),
        ("Export my inventory to Excel", "inventory_export"),
    ]
    for question, tool in questions:
        response = ask(world, owner, question)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["tool"] == tool and data["route"] == "document"
        assert data["artifacts"] and data["usage"]["llm_calls"] == 0
        for artifact in data["artifacts"]:
            download = owner.get(artifact["url"])
            assert download.status_code == 200
            assert download.headers["cache-control"] == "no-store"
            assert "attachment" in download.headers["content-disposition"]
            assert download.content.startswith(b"PK")


def test_export_followup_uses_actor_scoped_context(world, owner):
    cid = ask(world, owner, "Collect my invoices").json()["conversation_id"]
    assert ask(world, owner, "yes prepare them", cid).json()["tool"] == "invoice_export"
    assert ask(world, owner, "prepare it", cid, business="beta").json()["tool"] != "invoice_export"
    assert (
        intent("make a marketing plan", [{"role": "user", "content": "collect invoices"}]) is None
    )


def test_bad_period_asks_for_dates_instead_of_exporting_everything(world, owner):
    response = ask(world, owner, "Export my invoices for September 2026")
    assert response.status_code == 200
    assert not response.json()["artifacts"]
    assert "YYYY-MM-DD" in response.json()["answer"]
    bad = owner.get(
        f"/api/v1/admin/businesses/{world['ids']['acme']}/documents/invoices.xlsx?start=2026-11-01"
    )
    assert bad.status_code == 422


def test_date_interpretation_is_explicit():
    today = date(2026, 11, 20)
    assert requested_period("all invoices", today) is None
    assert requested_period("last month", today).start == date(2026, 10, 1)
    assert requested_period("last month", today).end == date(2026, 10, 31)
    assert requested_period("2026-10-01 to 2026-10-05", today).end == date(2026, 10, 5)
    assert requested_period("this financial year", today).start == date(2026, 4, 1)
    with pytest.raises(ApiError):
        requested_period("2026-02-30", today)
    with pytest.raises(ApiError):
        requested_period("2026-11-10 to 2026-11-01", today)


def test_invoice_exports_preserve_values_and_never_issue_invoices(world, owner, monkeypatch):
    customer = shop(world["gateway"])
    customer.csrf = shopper_login(customer)
    order = place(world, customer)
    prefix = f"/api/v1/admin/businesses/{world['ids']['acme']}/documents"
    assert len(sheet(owner.get(prefix + "/invoices.xlsx").content, 2)) == 1
    monkeypatch.setattr("billing_service.documents.utc_now", lambda: datetime(2026, 10, 31, 18, 45))
    assert act(world, owner, order["id"], "ship").status_code == 200
    with Session(world["engine"]) as db:
        inv = db.scalar(select(Invoice).where(Invoice.order_id == order["id"]))
        db.add(
            CreditNote(
                business_id=inv.business_id,
                invoice_id=inv.id,
                series="CN",
                fy=inv.fy,
                number=1,
                display_number="CN/26-27/00001",
                issued_at=datetime(2026, 12, 1),
                taxable_total=Decimal("10"),
                grand_total=Decimal("10"),
                content_hash="c" * 64,
            )
        )
        db.commit()
        number, fingerprint = inv.display_number, inv.content_hash
    data = owner.get(prefix + "/invoices.xlsx?start=2026-11-01&end=2026-11-01")
    assert data.status_code == 200
    invoices = sheet(data.content, 2)
    assert len(invoices) == 2 and invoices[1][0] == number
    assert invoices[1][14] == "1047.00" and invoices[1][15] == fingerprint
    assert len(sheet(data.content, 3)) == 3
    assert sheet(data.content, 4)[1][0] == "CN/26-27/00001"
    excluded = owner.get(prefix + "/invoices.xlsx?start=2026-10-31&end=2026-10-31")
    assert len(sheet(excluded.content, 2)) == 1
    packed = owner.get(prefix + "/invoices.zip")
    with ZipFile(BytesIO(packed.content)) as archive:
        assert "invoice-register.xlsx" in archive.namelist()
        records = json.loads(archive.read("records.json"))
        assert records[0]["content_hash"] == fingerprint
        assert records[0]["credit_notes"][0]["grand_total"] == "10.00"
        assert number in archive.read("invoices/invoice-00001.html").decode()
    beta = f"/api/v1/admin/businesses/{world['ids']['beta']}/documents/invoices.xlsx"
    assert len(sheet(owner.get(beta).content, 2)) == 1
    with Session(world["engine"]) as db:
        assert db.scalar(select(func.count()).select_from(Invoice)) == 1


def test_download_permissions_are_independent_of_chat(world, owner, monkeypatch):
    prefix = f"/api/v1/admin/businesses/{world['ids']['acme']}/documents"
    artifact = ask(world, owner, "Collect invoices").json()["artifacts"][0]["url"]
    anonymous = TestClient(world["gateway"], base_url=ORIGIN)
    assert anonymous.get(artifact).status_code == 401
    staff = TestClient(world["gateway"], base_url=ORIGIN)
    csrf = login(staff, email="bob@example.com").json()["csrf_token"]
    assert staff.get(artifact).status_code == 200  # existing STAFF permission permits invoice reads
    from blissbiz_common.permissions import ROLE_PERMISSIONS

    monkeypatch.setitem(ROLE_PERMISSIONS, "STAFF", ROLE_PERMISSIONS["STAFF"] - {"invoices.read"})
    assert staff.get(artifact).status_code == 403
    denied = staff.post(
        f"/api/v1/admin/businesses/{world['ids']['acme']}/business-guide",
        json={"text": "Collect invoices"},
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )
    assert denied.status_code == 403
    for kind in ("invoices.xlsx", "invoices.zip", "inventory.xlsx", "registration.xlsx"):
        url = f"/api/v1/admin/businesses/{world['ids']['beta']}/documents/{kind}"
        assert staff.get(url).status_code == 404
    assert (
        owner.get(prefix + "/invoices.xlsx", headers={"Origin": "https://evil.example"}).status_code
        == 403
    )
    assert owner.get(prefix + "/anything.xlsx").status_code == 422


def test_inventory_and_registration_workbooks(world, owner):
    prefix = f"/api/v1/admin/businesses/{world['ids']['acme']}/documents"
    rows = sheet(owner.get(prefix + "/inventory.xlsx").content, 1)
    skus = {row[1] for row in rows[1:]}
    assert "TEE-S" in skus and "BETA-1" not in skus
    tee = next(row for row in rows[1:] if row[1] == "TEE-S")
    assert Decimal(tee[8]) == Decimal(tee[6]) - Decimal(tee[7])
    registration = owner.get(prefix + "/registration.xlsx")
    assert "To provide" in str(sheet(registration.content, 3))
    assert "Not collected or verified" in str(sheet(registration.content, 2))
    assert "not a government form" in str(sheet(registration.content, 4))


def test_workbook_untrusted_text_is_never_a_formula():
    content = workbook(
        [
            (
                "Text",
                [
                    ["Description", "Amount"],
                    ['=HYPERLINK("https://evil")', Decimal("12.34")],
                    ["<script>\x00 & text", -4],
                ],
            )
        ]
    )
    with ZipFile(BytesIO(content)) as z:
        root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        assert not root.findall(".//s:f", NS)
        cells = root.findall(".//s:row", NS)[1].findall("s:c", NS)
        assert cells[0].attrib["t"] == "inlineStr"
        assert cells[1].find("s:v", NS).text == "12.34"


def test_registration_followup_tailors_draft_without_changing_profile(world, owner):
    response = ask(world, owner, "Prepare my GST registration documents")
    cid = response.json()["conversation_id"]
    followup = ask(world, owner, "My shop is in Karnataka, a proprietorship, and rented", cid)
    assert followup.status_code == 200
    data = followup.json()
    assert data["tool"] == "registration_pack"
    url = data["artifacts"][0]["url"]
    assert "state_code=29" in url and "premises=RENTED" in url
    download = owner.get(url)
    assert download.status_code == 200
    assert "Declared in chat; not verified" in str(sheet(download.content, 1))
    assert "Not selected for declared premises" in str(sheet(download.content, 2))
    assert "RENTED" in str(sheet(download.content, 3))
    assert "partnership deed/company papers not selected" in str(sheet(download.content, 2))


def test_period_followups_and_all_does_not_override_this_month(world, owner):
    response = ask(world, owner, "Collect all my invoices for this month")
    data = response.json()
    assert "start=" in data["artifacts"][0]["url"]
    copied = ask(world, owner, "make an excel", data["conversation_id"]).json()
    assert copied["artifacts"][0]["url"] == data["artifacts"][0]["url"]
    narrowed = ask(world, owner, "2026-10-01 to 2026-10-31", data["conversation_id"]).json()
    assert "start=2026-10-01&end=2026-10-31" in narrowed["artifacts"][0]["url"]
    assert ask(world, owner, "Create an Excel of my invoices").json()["tool"] == "invoice_export"
    assert ask(world, owner, "Create an invoice").json()["route"] == "guard"


def test_model_can_select_export_but_cannot_invent_its_scope(world, owner, monkeypatch):
    import httpx
    from agent_service.claude import Claude, ToolChoice
    from test_agent import gateway_with_agent

    def choose(self, system, question, tools, history=None):
        assert "invoice_export" in {tool["name"] for tool in tools}
        return ToolChoice(
            "invoice_export", {"question": "Collect invoices from 2026-01-01 to 2026-01-31"}
        )

    monkeypatch.setattr(Claude, "choose_tool", choose)
    gateway = gateway_with_agent(world, httpx.MockTransport(lambda request: httpx.Response(500)))
    client = TestClient(gateway, base_url=ORIGIN)
    client.cookies, client.csrf = owner.cookies, owner.csrf
    result = ask(world, client, "I need a tidy file for my accountant").json()
    assert result["tool"] == "invoice_export" and result["route"] == "document"
    assert "?" not in result["artifacts"][0]["url"]
    assert result["usage"]["llm_calls"] == 1
