"""Small, dependency-free XLSX snapshots. Text is always text, never a formula or hyperlink.

Export services own the data selection and calculations; this module only serializes cells.
"""

import re
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from xml.etree.ElementTree import Element, SubElement, tostring
from zipfile import ZIP_DEFLATED, ZipFile

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _xml(root):
    return tostring(root, encoding="utf-8", xml_declaration=True)


def _col(n):
    result = ""
    while n:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def workbook(sheets: list[tuple[str, list[list]]]) -> bytes:
    """Header row, frozen headings, filters, typed numbers, readable widths and wrapped text."""
    output = BytesIO()
    types = Element("Types", xmlns="http://schemas.openxmlformats.org/package/2006/content-types")
    SubElement(
        types,
        "Default",
        Extension="rels",
        ContentType="application/vnd.openxmlformats-package.relationships+xml",
    )
    SubElement(types, "Default", Extension="xml", ContentType="application/xml")
    for name, content in (("workbook", "sheet.main"), ("styles", "styles")):
        SubElement(
            types,
            "Override",
            PartName=f"/xl/{name}.xml",
            ContentType=f"application/vnd.openxmlformats-officedocument.spreadsheetml.{content}+xml",
        )
    book = Element(
        "workbook",
        xmlns=NS,
        attrib={"xmlns:r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"},
    )
    listing = SubElement(book, "sheets")
    rels = Element(
        "Relationships", xmlns="http://schemas.openxmlformats.org/package/2006/relationships"
    )
    relbase = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    rootrels = Element(
        "Relationships", xmlns="http://schemas.openxmlformats.org/package/2006/relationships"
    )
    SubElement(
        rootrels,
        "Relationship",
        Id="rId1",
        Type=relbase + "officeDocument",
        Target="xl/workbook.xml",
    )
    with ZipFile(output, "w", ZIP_DEFLATED) as z:
        for i, (name, rows) in enumerate(sheets, 1):
            SubElement(listing, "sheet", name=name, sheetId=str(i), attrib={"r:id": f"rId{i}"})
            SubElement(
                rels,
                "Relationship",
                Id=f"rId{i}",
                Type=relbase + "worksheet",
                Target=f"worksheets/sheet{i}.xml",
            )
            SubElement(
                types,
                "Override",
                PartName=f"/xl/worksheets/sheet{i}.xml",
                ContentType=(
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
                ),
            )
            root = Element("worksheet", xmlns=NS)
            view = SubElement(SubElement(root, "sheetViews"), "sheetView", workbookViewId="0")
            SubElement(
                view, "pane", ySplit="1", topLeftCell="A2", activePane="bottomLeft", state="frozen"
            )
            columns = SubElement(root, "cols")
            count = max((len(row) for row in rows), default=1)
            for c in range(count):
                width = min(
                    55,
                    max(
                        16,
                        max(
                            (len(str(row[c] or "")) for row in rows[:100] if c < len(row)),
                            default=16,
                        )
                        + 2,
                    ),
                )
                SubElement(
                    columns,
                    "col",
                    min=str(c + 1),
                    max=str(c + 1),
                    width=str(width),
                    customWidth="1",
                )
            data = SubElement(root, "sheetData")
            for r, row in enumerate(rows, 1):
                rowxml = SubElement(data, "row", r=str(r))
                for c, value in enumerate(row, 1):
                    cell = SubElement(rowxml, "c", r=f"{_col(c)}{r}", s="1" if r == 1 else "0")
                    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                        if isinstance(value, Decimal) and not value.is_finite():
                            raise ValueError("Non-finite workbook number")
                        cell.set("s", "2" if isinstance(value, (float, Decimal)) else "0")
                        SubElement(cell, "v").text = str(value)
                    else:
                        cell.set("t", "inlineStr")
                        if isinstance(value, (date, datetime)):
                            value = value.isoformat()
                        value = re.sub(
                            r"[\x00-\x08\x0b\x0c\x0e-\x1f]",
                            "",
                            str(value if value is not None else ""),
                        )[:32767]
                        SubElement(
                            SubElement(cell, "is"), "t", {"xml:space": "preserve"}
                        ).text = value
            if rows:
                SubElement(root, "autoFilter", ref=f"A1:{_col(count)}{len(rows)}")
            z.writestr(f"xl/worksheets/sheet{i}.xml", _xml(root))
        SubElement(rels, "Relationship", Id="styles", Type=relbase + "styles", Target="styles.xml")
        z.writestr("[Content_Types].xml", _xml(types))
        z.writestr("_rels/.rels", _xml(rootrels))
        z.writestr("xl/workbook.xml", _xml(book))
        z.writestr("xl/_rels/workbook.xml.rels", _xml(rels))
        z.writestr(
            "xl/styles.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2">
<font>
<sz val="11"/>
<name val="Calibri"/>
</font>
<font>
<b/>
<color rgb="FFFFFFFF"/>
<sz val="11"/>
<name val="Calibri"/>
</font>
</fonts>
<fills count="3">
<fill>
<patternFill patternType="none"/>
</fill>
<fill>
<patternFill patternType="gray125"/>
</fill>
<fill>
<patternFill patternType="solid">
<fgColor rgb="FF544178"/>
<bgColor indexed="64"/>
</patternFill>
</fill>
</fills>
<borders count="1">
<border/>
</borders>
<cellStyleXfs count="1">
<xf/>
</cellStyleXfs>
<cellXfs count="3">
<xf fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">
<alignment vertical="top" wrapText="1"/>
</xf>
<xf fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1">
<alignment vertical="top" wrapText="1"/>
</xf>
<xf fontId="0" fillId="0" borderId="0" numFmtId="4" xfId="0" applyNumberFormat="1"/>
</cellXfs>
<cellStyles count="1">
<cellStyle name="Normal" xfId="0" builtinId="0"/>
</cellStyles>
</styleSheet>""",
        )
    return output.getvalue()
