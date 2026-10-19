"""Import and export lists of tables to scan (Excel or CSV).

Accepts the original tool's ``Table_name_credentials.xlsx`` layout (sheet
"Credentials" with CredentialsAccountJson / Project_id / Dataset_id /
table_id) as well as looser layouts: header names are matched loosely, and a
single column of ``project.dataset.table`` references also works.
"""

from __future__ import annotations

import csv
import os
import re

from .core import TableTarget

ALIASES = {
    "project": {"projectid", "project", "gcpproject", "projectname"},
    "dataset": {"datasetid", "dataset", "datasetname", "schema"},
    "table": {"tableid", "table", "tablename", "tables"},
    "ref": {"tableref", "fulltablename", "fullname", "fullyqualifiedname", "reference", "tablepath"},
    "key_file": {"credentialsaccountjson", "keyfile", "serviceaccountjson", "serviceaccount",
                 "credentials", "credentialsjson", "jsonkey", "key"},
}
PREFERRED_SHEETS = ("credentials", "tables", "tablenames")
HEADERS = ["Project_id", "Dataset_id", "table_id", "CredentialsAccountJson"]


def _norm(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _cell(value) -> str:
    return "" if value is None else str(value).strip()


def _read_sheets(path: str) -> list[list[list[str]]]:
    """Each sheet as a list of rows of strings, preferred sheet first."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".csv", ".txt"):
        with open(path, newline="", encoding="utf-8-sig") as fh:
            return [[row for row in csv.reader(fh)]]

    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        preferred = [n for p in PREFERRED_SHEETS for n in wb.sheetnames if _norm(n) == p]
        names = preferred + [n for n in wb.sheetnames if n not in preferred]
        return [[[_cell(v) for v in row] for row in wb[n].iter_rows(values_only=True)] for n in names]
    finally:
        wb.close()


def load_targets(path: str) -> tuple[list[TableTarget], list[str]]:
    """Read tables from an Excel/CSV file. Returns (targets, warnings)."""
    base_dir = os.path.dirname(os.path.abspath(path))
    warnings: list[str] = []
    for rows in _read_sheets(path):
        rows = [r for r in rows if any(r)]
        if not rows:
            continue
        found, warn = _from_rows(rows, base_dir)
        warnings.extend(warn)
        if found:
            return list(dict.fromkeys(found)), warnings
    if not warnings:
        warnings.append("No tables found. Expected columns Project_id, Dataset_id, table_id "
                        "or one column of project.dataset.table names.")
    return [], warnings


def _from_rows(rows: list[list[str]], base_dir: str) -> tuple[list[TableTarget], list[str]]:
    header = rows[0]
    cols: dict[str, int] = {}
    for i, name in enumerate(header):
        n = _norm(name)
        for role, names in ALIASES.items():
            if n in names and role not in cols:
                cols[role] = i
    body = rows[1:]
    if "table" not in cols and "ref" not in cols:
        if header and header[0].count(".") >= 2:  # headerless list of references
            cols["ref"] = 0
            body = rows
        else:
            return [], []

    def get(row, role):
        i = cols.get(role)
        return row[i].strip() if i is not None and i < len(row) and row[i] else ""

    targets: list[TableTarget] = []
    warnings: list[str] = []
    for n, row in enumerate(body, start=2 if body is not rows else 1):
        table = get(row, "table") or get(row, "ref")
        if not table:
            continue
        key = get(row, "key_file")
        if key and not os.path.isabs(key):
            key = os.path.normpath(os.path.join(base_dir, key))
        try:
            targets.append(TableTarget.parse(table, key, get(row, "project"), get(row, "dataset")))
        except ValueError as exc:
            warnings.append(f"Row {n}: {exc}")
    return targets, warnings


def save_targets(path: str, targets: list[TableTarget]) -> None:
    """Write a list in the same layout ``load_targets`` reads."""
    rows = [[t.project, t.dataset, t.table, t.key_file] for t in targets]
    if path.lower().endswith(".csv"):
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(HEADERS)
            writer.writerows(rows)
        return

    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Tables"
    ws.append(HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        ws.append(row)
    for col, width in zip("ABCD", (28, 28, 40, 50)):
        ws.column_dimensions[col].width = width
    wb.save(path)
