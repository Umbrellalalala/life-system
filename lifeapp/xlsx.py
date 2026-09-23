"""最小 xlsx 写入器，不依赖 openpyxl / xlsxwriter。

xlsx 就是一个 zip 里塞几份 OOXML。这里只支持「多 sheet + 内联字符串 + 数值」，
够导出用；不读、不做样式和公式。之所以自己写：这两个库都没装，
而为了导出一个表格给 PyInstaller 打包再加一个体积不小的依赖不划算。
"""
from __future__ import annotations

import re
import zipfile
from xml.sax.saxutils import escape


def _col(n: int) -> str:
    """0 → A, 25 → Z, 26 → AA。"""
    s = ""
    n += 1
    while n:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


_SAFE = re.compile(r"[\[\]:*?/\\]")


def safe_sheet_name(name: str, used: set[str]) -> str:
    """Excel 的 sheet 名：≤31 字符、不能带 []:*?/\\，且不能重复。"""
    base = _SAFE.sub(" ", (name or "").strip()) or "Sheet"
    base = base[:31]
    cand, i = base, 1
    while cand in used:
        i += 1
        suffix = f"({i})"
        cand = base[:31 - len(suffix)] + suffix
    used.add(cand)
    return cand


def _cell_xml(ref: str, value) -> str:
    if value is None or value == "":
        return f'<c r="{ref}"/>'
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"><v>{value}</v></c>'
    text = escape(str(value))
    # 换行要留着，A1 那种「基本信息」多行块全靠它
    return (f'<c r="{ref}" t="inlineStr" xml:space="preserve">'
            f'<is><t>{text}</t></is></c>')


def _sheet_xml(rows: list[list]) -> str:
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/'
             'spreadsheetml/2006/main">',
             '<sheetData>']
    for r, row in enumerate(rows, start=1):
        parts.append(f'<row r="{r}">')
        for c, val in enumerate(row):
            parts.append(_cell_xml(f"{_col(c)}{r}", val))
        parts.append('</row>')
    parts.append('</sheetData></worksheet>')
    return "".join(parts)


_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="1"><fill><patternFill patternType="none"/></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
</styleSheet>"""


def write_xlsx(path: str, sheets: list[tuple[str, list[list]]]) -> None:
    """sheets = [(sheet 名, [[单元格, ...], ...]), ...]"""
    used: set[str] = set()
    names = [(safe_sheet_name(n, used), rows) for n, rows in sheets] or [("Sheet1", [])]

    ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
          'content-types">',
          # 这两个 Default 是 OPC 规定的最小集；漏了 Excel 会报「文件已损坏」
          '<Default Extension="rels" ContentType="application/vnd.'
          'openxmlformats-package.relationships+xml"/>',
          '<Default Extension="xml" ContentType="application/xml"/>',
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
          'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.'
          'openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    for i in range(len(names)):
        ct.append(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" '
                  'ContentType="application/vnd.openxmlformats-officedocument.'
                  'spreadsheetml.worksheet+xml"/>')
    ct.append('</Types>')

    wb = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
          'relationships"><sheets>']
    wb_rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
               '<Relationships xmlns="http://schemas.openxmlformats.org/'
               'package/2006/relationships">']
    for idx, (name, rows) in enumerate(names, start=1):
        wb.append(f'<sheet name="{escape(name)}" sheetId="{idx}" '
                  f'r:id="rId{idx}"/>')
        wb_rels.append(
            f'<Relationship Id="rId{idx}" Type="http://schemas.openxmlformats.org/'
            f'officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{idx}.xml"/>')
    wb.append('</sheets><definedNames/></workbook>')
    n = len(names)
    wb_rels.append(f'<Relationship Id="rId{n + 1}" Type="http://schemas.'
                   'openxmlformats.org/officeDocument/2006/relationships/styles" '
                   'Target="styles.xml"/>')
    wb_rels.append('</Relationships>')

    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
                 '2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
                 'officeDocument/2006/relationships/officeDocument" '
                 'Target="xl/workbook.xml"/></Relationships>')

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "".join(ct))
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", "".join(wb))
        z.writestr("xl/_rels/workbook.xml.rels", "".join(wb_rels))
        z.writestr("xl/styles.xml", _STYLES)
        for i, (_name, rows) in enumerate(names, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(rows))
