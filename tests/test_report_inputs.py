from datetime import datetime

import pandas as pd
from openpyxl import load_workbook

from nci.core import Scanner, TableTarget
from nci.inputs import load_targets, save_targets
from nci.report import RunInfo, write_log_pdf, write_report
from tests.fakes import FakeClient
from tests.test_core import ROWS, SCHEMA


def _results():
    full = TableTarget("p", "d", "a_very_long_table_name_that_exceeds_excel_limits")
    dup = TableTarget("p", "d2", "a_very_long_table_name_that_exceeds_excel_limits")
    clean = TableTarget("p", "d", "clean")
    client = FakeClient({
        full.ref: (SCHEMA, ROWS),
        dup.ref: (SCHEMA, ROWS),
        clean.ref: (SCHEMA[:1], ROWS),
    })
    scanner = Scanner(lambda t: client)
    missing = TableTarget("p", "d", "missing")
    return [scanner.scan(t) for t in (full, dup, clean, missing)]


def test_report_sheets_and_content(tmp_path):
    path = tmp_path / "r.xlsx"
    write_report(str(path), _results(), RunInfo("tester", datetime(2026, 1, 1), datetime(2026, 1, 1, 0, 1)))
    wb = load_workbook(path)
    assert wb.sheetnames[:2] == ["Overview", "Null Columns"]
    assert wb.sheetnames[-1] == "Run Info"
    detail = wb.sheetnames[2:-1]
    assert len(detail) == 4 and len(set(detail)) == 4
    assert all(len(n) <= 31 for n in detail)

    ov = list(wb["Overview"].iter_rows(values_only=True))
    assert ov[0][:4] == ("Project", "Dataset", "Table", "Status")
    # Fully populated table is still listed (100% coverage).
    clean = next(r for r in ov if r[2] == "clean")
    assert clean[3] == "Scanned" and clean[6] == 0 and clean[8] == 1.0
    missing = next(r for r in ov if r[2] == "missing")
    assert missing[3] == "Failed"
    # Links go to the right sheet.
    link = wb["Overview"].cell(2, 12).hyperlink
    assert link.location == f"'{detail[0]}'!A1"

    nulls = list(wb["Null Columns"].iter_rows(values_only=True))
    assert len(nulls) == 1 + 5 + 5


def test_log_pdf(tmp_path):
    out = write_log_pdf("line 1\nunicode → ok\n", str(tmp_path / "l.pdf"))
    assert open(out, "rb").read(4) == b"%PDF"


def test_load_original_layout(tmp_path):
    key = tmp_path / "key.json"
    key.write_text("{}")
    path = tmp_path / "Table_name_credentials.xlsx"
    with pd.ExcelWriter(path) as w:
        pd.DataFrame({"x": [1]}).to_excel(w, sheet_name="Tablenames", index=False)
        pd.DataFrame({
            "CredentialsAccountJson": ["key.json", "key.json", None],
            "Project_id": ["p1", "p1", "p2"],
            "Dataset_id": ["d1", "d1", "d2"],
            "table_id": ["t1", "t2", None],
        }).to_excel(w, sheet_name="Credentials", index=False)
    targets, warnings = load_targets(str(path))
    assert [t.ref for t in targets] == ["p1.d1.t1", "p1.d1.t2"]
    assert targets[0].key_file == str(key)
    assert warnings == []


def test_load_refs_csv_and_roundtrip(tmp_path):
    csv = tmp_path / "list.csv"
    csv.write_text("p.d.t1\np.d.t2\nbad\n")
    targets, warnings = load_targets(str(csv))
    assert [t.ref for t in targets] == ["p.d.t1", "p.d.t2"]
    assert len(warnings) == 1

    out = tmp_path / "out.xlsx"
    save_targets(str(out), targets)
    again, _ = load_targets(str(out))
    assert again == targets
