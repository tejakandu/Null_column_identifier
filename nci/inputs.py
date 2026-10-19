"""Import and export lists of tables to scan (Excel or CSV).

Accepts the original tool's ``Table_name_credentials.xlsx`` layout (sheet
"Credentials" with CredentialsAccountJson / Project_id / Dataset_id /
table_id) as well as looser layouts: header names are matched loosely, and a
single column of ``project.dataset.table`` references also works.
"""

from __future__ import annotations

import os
import re

import pandas as pd

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


def _norm(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _cell(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def load_targets(path: str) -> tuple[list[TableTarget], list[str]]:
    """Read tables from an Excel/CSV file. Returns (targets, warnings)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".csv", ".txt"):
        frames = [pd.read_csv(path, dtype=str, keep_default_na=False)]
    else:
        sheets = pd.read_excel(path, sheet_name=None, dtype=str)
        by_norm = {_norm(k): v for k, v in sheets.items()}
        chosen = next((by_norm[s] for s in PREFERRED_SHEETS if s in by_norm), None)
        frames = [chosen] if chosen is not None else list(sheets.values())

    base_dir = os.path.dirname(os.path.abspath(path))
    targets: list[TableTarget] = []
    warnings: list[str] = []
    for df in frames:
        found, warn = _from_frame(df, base_dir)
        targets.extend(found)
        warnings.extend(warn)
        if found:
            break

    unique = list(dict.fromkeys(targets))
    if not unique and not warnings:
        warnings.append("No tables found. Expected columns Project_id, Dataset_id, table_id "
                        "or one column of project.dataset.table names.")
    return unique, warnings


def _from_frame(df: pd.DataFrame, base_dir: str) -> tuple[list[TableTarget], list[str]]:
    cols: dict[str, str] = {}
    for col in df.columns:
        n = _norm(col)
        for role, names in ALIASES.items():
            if n in names and role not in cols:
                cols[role] = col

    rows = df.to_dict("records")
    if "table" not in cols and "ref" not in cols:
        # Headerless single column of references: treat the header as a row too.
        if len(df.columns) >= 1 and _looks_like_ref(df.columns[0]):
            first = df.columns[0]
            cols["ref"] = first
            rows = [{first: first}] + rows
        else:
            return [], []

    targets: list[TableTarget] = []
    warnings: list[str] = []
    for n, row in enumerate(rows, start=2):
        key = _cell(row.get(cols.get("key_file", ""), ""))
        if key and not os.path.isabs(key):
            key = os.path.normpath(os.path.join(base_dir, key))
        project = _cell(row.get(cols.get("project", ""), ""))
        dataset = _cell(row.get(cols.get("dataset", ""), ""))
        table = _cell(row.get(cols.get("table", ""), "")) or _cell(row.get(cols.get("ref", ""), ""))
        if not table:
            continue
        try:
            targets.append(TableTarget.parse(table, key, project, dataset))
        except ValueError as exc:
            warnings.append(f"Row {n}: {exc}")
    return targets, warnings


def _looks_like_ref(text) -> bool:
    return isinstance(text, str) and text.count(".") >= 2


def save_targets(path: str, targets: list[TableTarget]) -> None:
    """Write a list in the same layout ``load_targets`` reads."""
    df = pd.DataFrame(
        [{"Project_id": t.project, "Dataset_id": t.dataset, "table_id": t.table,
          "CredentialsAccountJson": t.key_file} for t in targets],
        columns=["Project_id", "Dataset_id", "table_id", "CredentialsAccountJson"])
    if path.lower().endswith(".csv"):
        df.to_csv(path, index=False)
    else:
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Tables", index=False)
            ws = writer.sheets["Tables"]
            for col, width in zip("ABCD", (28, 28, 40, 50)):
                ws.column_dimensions[col].width = width
