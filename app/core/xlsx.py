"""Минимальная запись .xlsx без сторонних библиотек (для выгрузки заказов в Excel).

Умеет: несколько листов, текст и числа, жирная шапка, закреплённая первая строка, фильтр и ширину колонок.
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
{sheets}</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFE8EFE3"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="3">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _col(n: int) -> str:
    """0 → A, 25 → Z, 26 → AA."""
    name = ""
    n += 1
    while n:
        n, rem = divmod(n - 1, 26)
        name = chr(65 + rem) + name
    return name


def _clean(text: str) -> str:
    """Убираем управляющие символы, которые XML не допускает."""
    return "".join(ch for ch in text if ch in "\t\n\r" or ord(ch) >= 32)


def _sheet_xml(rows: list[list]) -> str:
    widths: list[int] = []
    for row in rows:
        for i, value in enumerate(row):
            length = max((len(line) for line in str(value if value is not None else "").split("\n")), default=0)
            if i >= len(widths):
                widths.append(10)
            widths[i] = max(widths[i], min(60, length + 2))
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths))
    body = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            if value is None or value == "":
                continue
            ref = f"{_col(c)}{r}"
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                style = ' s="1"' if r == 1 else ' s="2"' if "\n" in str(value) else ""
                cells.append(f'<c r="{ref}" t="inlineStr"{style}><is><t xml:space="preserve">'
                             f"{escape(_clean(str(value)))}</t></is></c>")
            else:
                cells.append(f'<c r="{ref}"><v>{value!r}</v></c>')
        body.append(f'<row r="{r}">{"".join(cells)}</row>')
    last = f"{_col(max(len(widths), 1) - 1)}{max(len(rows), 1)}"
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
            'state="frozen"/></sheetView></sheetViews>'
            f'<cols>{cols}</cols><sheetData>{"".join(body)}</sheetData>'
            f'<autoFilter ref="A1:{last}"/></worksheet>')


def write_xlsx(path: str | Path, sheets: dict[str, list[list]]) -> Path:
    """sheets: {«название листа»: [шапка, строка, строка…]}. Возвращает путь к файлу."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    names = list(sheets) or ["Лист1"]
    content = _CONTENT_TYPES.format(sheets="".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>\n'
        for i in range(1, len(names) + 1)))
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                + "".join(f'<sheet name="{escape(_clean(n)[:31], {chr(34): "&quot;"})}" sheetId="{i}" r:id="rId{i}"/>'
                          for i, n in enumerate(names, start=1)) + "</sheets></workbook>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                      f'relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(names) + 1))
            + f'<Relationship Id="rId{len(names) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
              'relationships/styles" Target="styles.xml"/></Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/styles.xml", _STYLES)
        for i, name in enumerate(names, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(sheets.get(name) or [[]]))
    return path
