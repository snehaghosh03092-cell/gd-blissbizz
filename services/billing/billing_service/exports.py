"""Read-only invoice snapshots from billing-owned tables. No invoice is issued by an export."""

import html
import json
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from app.db.models.billing import CreditNote, CreditNoteLine, Invoice, InvoiceLine
from blissbiz_common.errors import ApiError
from blissbiz_common.workbooks import workbook
from sqlalchemy import select

MAX_INVOICES = 5000
MAX_LINES = 50000
IST = timedelta(hours=5, minutes=30)
AMOUNTS = (
    "taxable_total",
    "cgst_total",
    "sgst_total",
    "igst_total",
    "cess_total",
    "round_off",
    "grand_total",
)


def records(db, business_id: str, start: date | None, end: date | None):
    from billing_service.documents import credit_note_out, invoice_out

    if bool(start) != bool(end) or (start and start > end):
        raise ApiError(
            422, "invalid_period", "Supply both start and end dates, with start no later than end."
        )
    query = select(Invoice).where(Invoice.business_id == business_id, Invoice.status == "ISSUED")
    if start:
        query = query.where(
            Invoice.issued_at >= datetime.combine(start, time()) - IST,
            Invoice.issued_at < datetime.combine(end + timedelta(days=1), time()) - IST,
        )
    invoices = db.scalars(
        query.order_by(Invoice.issued_at, Invoice.id).limit(MAX_INVOICES + 1)
    ).all()
    if len(invoices) > MAX_INVOICES:
        raise ApiError(
            422, "export_too_large", "More than 5,000 invoices. Choose a smaller date range."
        )
    ids = [i.id for i in invoices]
    if not ids:
        return []
    lines = db.scalars(
        select(InvoiceLine)
        .where(InvoiceLine.business_id == business_id, InvoiceLine.invoice_id.in_(ids))
        .order_by(InvoiceLine.invoice_id, InvoiceLine.position)
        .limit(MAX_LINES + 1)
    ).all()
    notes = db.scalars(
        select(CreditNote)
        .where(CreditNote.business_id == business_id, CreditNote.invoice_id.in_(ids))
        .order_by(CreditNote.issued_at, CreditNote.id)
        .limit(MAX_LINES + 1)
    ).all()
    if len(lines) > MAX_LINES or len(notes) > MAX_LINES:
        raise ApiError(
            422, "export_too_large", "Too many document lines. Choose a smaller date range."
        )
    note_ids = [n.id for n in notes]
    note_lines = (
        db.scalars(
            select(CreditNoteLine)
            .where(
                CreditNoteLine.business_id == business_id,
                CreditNoteLine.credit_note_id.in_(note_ids),
            )
            .order_by(CreditNoteLine.id)
            .limit(MAX_LINES + 1)
        ).all()
        if note_ids
        else []
    )
    if len(note_lines) > MAX_LINES:
        raise ApiError(
            422, "export_too_large", "Too many credit-note lines. Choose a smaller date range."
        )
    by_invoice, notes_by_invoice, by_note = defaultdict(list), defaultdict(list), defaultdict(list)
    for line in lines:
        by_invoice[line.invoice_id].append(line)
    for line in note_lines:
        by_note[line.credit_note_id].append(line)
    for note in notes:
        notes_by_invoice[note.invoice_id].append(credit_note_out(note, by_note[note.id]))
    result = []
    for inv in invoices:
        data = invoice_out(inv, by_invoice[inv.id])
        data["credit_notes"] = notes_by_invoice[inv.id]
        result.append(data)
    return result


def invoice_workbook(items: list[dict], start: date | None, end: date | None) -> bytes:
    headers = [
        "Invoice",
        "Document type",
        "Order ID",
        "Issued UTC",
        "Seller GSTIN",
        "Buyer GSTIN",
        "Place of supply",
        "Inter-state",
        "Taxable INR",
        "CGST INR",
        "SGST INR",
        "IGST INR",
        "Cess INR",
        "Round off INR",
        "Grand total INR",
        "Fingerprint",
    ]
    invoices = [headers]
    lines = [
        [
            "Invoice",
            "Position",
            "Description",
            "HSN/SAC",
            "Quantity",
            "Unit",
            "Unit price INR",
            "Discount INR",
            "Taxable INR",
            "CGST %",
            "CGST INR",
            "SGST %",
            "SGST INR",
            "IGST %",
            "IGST INR",
            "Cess INR",
            "Line total INR",
        ]
    ]
    notes = [
        [
            "Credit note",
            "Invoice",
            "Issued UTC",
            "Reason",
            "Taxable INR",
            "CGST INR",
            "SGST INR",
            "IGST INR",
            "Cess INR",
            "Round off INR",
            "Grand total INR",
        ]
    ]
    note_lines = [
        [
            "Credit note",
            "Invoice",
            "Description",
            "HSN/SAC",
            "Quantity",
            "Taxable INR",
            "CGST INR",
            "SGST INR",
            "IGST INR",
            "Cess INR",
            "Line total INR",
        ]
    ]
    for item in items:
        invoices.append(
            [
                item["number"],
                item["doc_type"],
                item["order_id"],
                item["issued_at"],
                item["seller"].get("gstin"),
                item["buyer"].get("gstin"),
                item["place_of_supply_state"],
                "Yes" if item["is_inter_state"] else "No",
                *[Decimal(item[k]) for k in AMOUNTS],
                item["content_hash"],
            ]
        )
        for line in item["lines"]:
            lines.append(
                [
                    item["number"],
                    line["position"],
                    line["description"],
                    line["hsn_sac"],
                    Decimal(line["quantity"]),
                    line["unit"],
                    *[
                        Decimal(line[k])
                        for k in (
                            "unit_price",
                            "discount",
                            "taxable_value",
                            "cgst_rate",
                            "cgst",
                            "sgst_rate",
                            "sgst",
                            "igst_rate",
                            "igst",
                            "cess",
                            "line_total",
                        )
                    ],
                ]
            )
        for note in item["credit_notes"]:
            notes.append(
                [
                    note["number"],
                    item["number"],
                    note["issued_at"],
                    note["reason"],
                    *[Decimal(note[k]) for k in AMOUNTS],
                ]
            )
            for line in note["lines"]:
                note_lines.append(
                    [
                        note["number"],
                        item["number"],
                        line["description"],
                        line["hsn_sac"],
                        Decimal(line["quantity"]),
                        *[
                            Decimal(line[k])
                            for k in ("taxable_value", "cgst", "sgst", "igst", "cess", "line_total")
                        ],
                    ]
                )
    summary = [
        ["Measure", "Value"],
        ["Exported UTC", datetime.now(UTC).isoformat(timespec="seconds")],
        [
            "Invoice issue dates (India)",
            f"{start} through {end}" if start else "All issued invoices",
        ],
        ["Invoice count", len(items)],
        ["Credit note count", len(notes) - 1],
        [
            "Invoice total INR (before credits)",
            sum((Decimal(i["grand_total"]) for i in items), Decimal(0)),
        ],
        [
            "Credit total INR",
            sum((Decimal(n["grand_total"]) for i in items for n in i["credit_notes"]), Decimal(0)),
        ],
        [
            "Coverage",
            "Issued sales invoices/bills of supply and all credit notes linked"
            " to them, even notes issued outside the selected dates. Supplier "
            "purchase invoices are not included.",
        ],
        [
            "Dates",
            "Filter applies to invoice issue date in India time, not order "
            "placement. Timestamps in detail sheets are UTC.",
        ],
        [
            "Use",
            "Accounting support export; not a GSTR upload, net tax liability "
            "calculation, input-credit reconciliation or registration "
            "application.",
        ],
        [
            "Documents",
            "No invoices created or amended. An order without an issued "
            "invoice is absent. Download again for a new snapshot.",
        ],
    ]
    return workbook(
        [
            ("Summary", summary),
            ("Invoices", invoices),
            ("Invoice lines", lines),
            ("Credit notes", notes),
            ("Credit note lines", note_lines),
        ]
    )


def printable(item: dict) -> str:
    """Escaped invoice copy with the same immutable values as the app's printable screen."""
    esc = lambda value: html.escape(str(value if value is not None else ""))  # noqa: E731

    def table(headers, rows):
        return (
            "<table><thead><tr>"
            + "".join(f"<th>{esc(v)}</th>" for v in headers)
            + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f"<td>{esc(v)}</td>" for v in row) + "</tr>" for row in rows)
            + "</tbody></table>"
        )

    def party(label, data):
        return f"<h2>{label}</h2>" + "<br>".join(
            esc(data.get(k))
            for k in ("legal_name", "name", "address", "gstin", "state_code")
            if data.get(k)
        )

    fields = (
        "position",
        "description",
        "hsn_sac",
        "quantity",
        "unit",
        "unit_price",
        "discount",
        "taxable_value",
        "cgst_rate",
        "cgst",
        "sgst_rate",
        "sgst",
        "igst_rate",
        "igst",
        "cess",
        "line_total",
    )
    body = (
        f"<h1>{esc(item['doc_type'].replace('_', ' ').title())}</h1>"
        f"<p>{esc(item['number'])} · {esc(item['issued_at'])} UTC</p>"
    )
    body += party("Seller", item["seller"]) + party("Buyer", item["buyer"])
    body += (
        f"<p>Order: {esc(item['order_id'])} · "
        f"Place of supply: {esc(item['place_of_supply_state'])} · "
        f"Inter-state: {esc(item['is_inter_state'])}</p>"
    )
    body += table(
        [f.replace("_", " ").title() for f in fields],
        [[line.get(f) for f in fields] for line in item["lines"]],
    )
    body += table(["Total (INR)", "Amount"], [[k.replace("_", " "), item[k]] for k in AMOUNTS])
    for note in item["credit_notes"]:
        body += (
            f"<h2>Credit note {esc(note['number'])}</h2>"
            f"<p>{esc(note['issued_at'])} UTC · {esc(note['reason'])}</p>"
        )
        body += table(["Total (INR)", "Amount"], [[k.replace("_", " "), note[k]] for k in AMOUNTS])
    body += (
        f"<p>Prices include tax: {esc(item['prices_include_tax'])}. "
        f"Fingerprint: {esc(item['content_hash'])}</p>"
    )
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta '
        'name="viewport" content="width=device-width"><title>Invoice '
        "copy</title><style>body{font:14px "
        "system-ui;margin:32px;color:#222}table{border-collapse:collapse;width:100%;margin:24px"
        " 0}td,th{border:1px solid "
        "#ddd;padding:8px;text-align:left}th{background:#f3f0fa}@media "
        "print{body{margin:0;font-size:10px}}</style><body>" + body + "</body></html>"
    )


def invoice_bundle(items, start, end) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as bundle:
        bundle.writestr("invoice-register.xlsx", invoice_workbook(items, start, end))
        bundle.writestr("records.json", json.dumps(items, ensure_ascii=False, indent=2))
        bundle.writestr(
            "README.txt",
            "Existing issued sales documents only. Open the HTML copies in a "
            "browser and Print / Save as PDF. No PDF originals are fabricated."
            " The JSON includes full line items and credit-note details. The "
            "workbook explains date coverage. This is not a GST return or a "
            "complete registration application.\n",
        )
        for i, item in enumerate(items, 1):
            bundle.writestr(f"invoices/invoice-{i:05d}.html", printable(item))
    return output.getvalue()
