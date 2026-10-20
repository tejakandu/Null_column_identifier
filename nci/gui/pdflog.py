"""PDF copy of the run log, drawn with Qt (no extra libraries).

Layout: title and credits, run details, one row per table, null columns,
then the full log. Every page has a footer with the credits, the run id and
"Page X of Y".
"""

from __future__ import annotations

import html

from PySide6.QtCore import QMarginsF, QRectF, QSizeF, Qt
from PySide6.QtGui import QColor, QFont, QPageLayout, QPageSize, QPainter, QPdfWriter, QTextDocument

from .. import APP_NAME, CREDITS
from ..core import TableResult, format_bytes
from ..report import RunInfo

ACCENT = "#1F4E79"
FOOTER_MM = 9
MAX_NULL_LIST = 5000  # null columns listed by name; the Excel report has all


def _status(res: TableResult) -> tuple[str, str]:
    if res.status == "ok":
        if res.null_columns:
            return f"{len(res.null_columns)} null", "#B26A00"
        return "No null columns", "#2E7D32"
    if res.status == "cancelled":
        return "Cancelled", "#6B7280"
    return "Failed", "#C4314B"


def build_html(info: RunInfo, results: list[TableResult], log_lines: list[str],
               report_path: str = "") -> str:
    e = html.escape
    details = info.details(results)
    if report_path:
        details.append(("Excel report", report_path))
    detail_rows = "".join(
        f"<tr><td width='28%' style='color:#374151'><b>{e(k)}</b></td><td>{e(v)}</td></tr>"
        for k, v in details)

    table_rows = []
    for r in results:
        text, color = _status(r)
        cov = f"{r.coverage:.1%}" if r.coverage is not None else ""
        rows = f"{r.total_rows:,}" if r.total_rows is not None else ""
        cols = str(len(r.columns)) if r.columns else ""
        note = f"<br/><span style='color:#6B7280'>{e(r.error)}</span>" if r.status == "error" else ""
        scanned = e(format_bytes(r.bytes_processed)) if r.bytes_processed else ""
        table_rows.append(
            f"<tr><td>{e(r.target.ref)}</td><td align='right'>{rows}</td><td align='right'>{cols}</td>"
            f"<td align='right'>{cov}</td><td style='color:{color}'><b>{e(text)}</b>{note}</td>"
            f"<td align='right'>{scanned}</td></tr>")

    nulls = []
    count = 0
    for r in results:
        if not r.null_columns:
            continue
        names = []
        for c in r.null_columns:
            if count >= MAX_NULL_LIST:
                break
            names.append(e(c.name))
            count += 1
        nulls.append(f"<p style='margin:6px 0 2px 0'><b>{e(r.target.ref)}</b> "
                     f"<span style='color:#6B7280'>({len(r.null_columns)})</span><br/>"
                     f"<span style='font-family:Consolas,Courier New,monospace'>{', '.join(names)}</span></p>")
    if count >= MAX_NULL_LIST:
        nulls.append("<p style='color:#6B7280'>… more in the Excel report.</p>")
    if not nulls:
        nulls.append("<p style='color:#2E7D32'>No null columns were found.</p>")

    log = e("\n".join(log_lines))
    th = f"style='background:{ACCENT};color:white'"
    return f"""
<html><body style="font-family:'Segoe UI',Arial,sans-serif; font-size:9pt; color:#1F2328">
<table width="100%" cellspacing="0" cellpadding="10" style="background:{ACCENT}">
<tr><td>
<span style="color:white; font-size:17pt; font-weight:600">{e(APP_NAME)}</span><br/>
<span style="color:#DCE6F2; font-size:10pt">Run log · {e(info.started.strftime('%d %b %Y, %H:%M') if info.started else '')}</span><br/>
<span style="color:#DCE6F2; font-size:9pt">{e(CREDITS)}</span>
</td></tr></table>

<h3 style="color:{ACCENT}; margin-top:14px">Run details</h3>
<table width="100%" cellspacing="0" cellpadding="4" border="0.5" style="border-color:#D0D7DE">
{detail_rows}
</table>

<h3 style="color:{ACCENT}; margin-top:14px">Tables</h3>
<table width="100%" cellspacing="0" cellpadding="4" border="0.5" style="border-color:#D0D7DE">
<tr><th align="left" width="40%" {th}>Table</th><th align="right" {th}>Rows</th><th align="right" {th}>Columns</th>
<th align="right" {th}>Coverage</th><th align="left" {th}>Result</th><th align="right" {th}>Scanned</th></tr>
{''.join(table_rows) or '<tr><td colspan="6">No tables.</td></tr>'}
</table>

<h3 style="color:{ACCENT}; margin-top:14px">Null columns</h3>
{''.join(nulls)}

<h3 style="color:{ACCENT}; margin-top:14px">Full log</h3>
<pre style="font-family:Consolas,'Courier New',monospace; font-size:7.5pt; white-space:pre-wrap">{log}</pre>
</body></html>"""


def write_pdf_log(path: str, info: RunInfo, results: list[TableResult], log_lines: list[str],
                  report_path: str = "") -> str:
    writer = QPdfWriter(path)
    writer.setTitle(f"{APP_NAME} run log {info.run_id}")
    writer.setCreator(f"{APP_NAME} · {CREDITS}")
    writer.setResolution(144)
    writer.setPageLayout(QPageLayout(QPageSize(QPageSize.A4), QPageLayout.Portrait,
                                     QMarginsF(14, 14, 14, 10), QPageLayout.Millimeter))
    paint = writer.pageLayout().paintRectPixels(writer.resolution())
    footer_h = FOOTER_MM / 25.4 * writer.resolution()
    width, body_h = paint.width(), paint.height() - footer_h

    doc = QTextDocument()
    doc.documentLayout().setPaintDevice(writer)
    doc.setDocumentMargin(0)
    doc.setHtml(build_html(info, results, log_lines, report_path))
    doc.setPageSize(QSizeF(width, body_h))
    pages = max(1, doc.pageCount())

    painter = QPainter(writer)
    try:
        footer_font = QFont("Segoe UI")
        footer_font.setPointSizeF(7.5)
        for page in range(pages):
            if page:
                writer.newPage()
            painter.save()
            painter.translate(0, -page * body_h)
            doc.drawContents(painter, QRectF(0, page * body_h, width, body_h))
            painter.restore()

            y = body_h + footer_h * 0.25
            painter.setPen(QColor("#D0D7DE"))
            painter.drawLine(0, int(y), int(width), int(y))
            painter.setPen(QColor("#6B7280"))
            painter.setFont(footer_font)
            rect = QRectF(0, y + 4, width, footer_h - 6)
            painter.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, CREDITS)
            painter.drawText(rect, Qt.AlignCenter, f"Run {info.run_id[:8]}" if info.run_id else "")
            painter.drawText(rect, Qt.AlignRight | Qt.AlignVCenter, f"Page {page + 1} of {pages}")
    finally:
        painter.end()
    return path
