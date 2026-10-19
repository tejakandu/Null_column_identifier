import threading

import pytest
from google.cloud.bigquery import SchemaField

from nci.core import Scanner, TableTarget, build_query, flatten_schema
from tests.fakes import FakeClient

SCHEMA = [
    SchemaField("id", "INTEGER", "REQUIRED"),
    SchemaField("empty", "STRING"),
    SchemaField("select", "STRING"),  # reserved word
    SchemaField("tags", "STRING", "REPEATED"),
    SchemaField("addr", "RECORD", fields=[
        SchemaField("city", "STRING"),
        SchemaField("zip", "STRING"),
    ]),
    SchemaField("items", "RECORD", "REPEATED", fields=[
        SchemaField("sku", "STRING"),
        SchemaField("note", "STRING"),
        SchemaField("parts", "RECORD", "REPEATED", fields=[
            SchemaField("serial", "STRING"),
            SchemaField("color", "STRING"),
        ]),
    ]),
]

ROWS = [
    {"id": 1, "empty": None, "select": "x", "tags": [], "addr": {"city": "A", "zip": None},
     "items": [{"sku": "s1", "note": None, "parts": [{"serial": "p1", "color": None}]}]},
    {"id": 2, "empty": None, "select": None, "tags": [], "addr": None, "items": []},
]

T = TableTarget("proj-1", "ds", "tbl")


def make_scanner(client, **kw):
    return Scanner(lambda target: client, **kw)


def test_flatten_all_leaves():
    names = [c.name for c in flatten_schema(SCHEMA)]
    assert names == ["id", "empty", "select", "tags", "addr.city", "addr.zip",
                     "items.sku", "items.note", "items.parts.serial", "items.parts.color"]


def test_predicates_nest_unnest():
    spec = {c.name: c for c in flatten_schema(SCHEMA)}
    assert spec["select"].predicate == "t.`select` IS NOT NULL"
    assert spec["tags"].predicate == "ARRAY_LENGTH(t.`tags`) > 0"
    assert spec["addr.zip"].predicate == "t.`addr`.`zip` IS NOT NULL"
    assert spec["items.parts.color"].predicate == (
        "EXISTS(SELECT 1 FROM UNNEST(t.`items`) AS _u0 WHERE "
        "EXISTS(SELECT 1 FROM UNNEST(_u0.`parts`) AS _u1 WHERE _u1.`color` IS NOT NULL))")


def test_build_query_single_scan():
    sql = build_query(T, flatten_schema(SCHEMA))
    assert sql.startswith("SELECT COUNT(*) AS total_rows")
    assert sql.count("COUNTIF(") == 10
    assert sql.endswith("FROM `proj-1`.`ds`.`tbl` AS t")


def test_scan_finds_null_columns():
    client = FakeClient({T.ref: (SCHEMA, ROWS)})
    res = make_scanner(client).scan(T)
    assert res.status == "ok"
    assert res.total_rows == 2
    assert [c.name for c in res.null_columns] == [
        "empty", "tags", "addr.zip", "items.note", "items.parts.color"]
    assert res.coverage == pytest.approx(0.5)
    assert len(client.queries) == 1


def test_batches_and_progress():
    client = FakeClient({T.ref: (SCHEMA, ROWS)})
    seen = []
    res = make_scanner(client, batch_size=3).scan(T, on_progress=lambda d, t: seen.append((d, t)))
    assert len(client.queries) == 4
    assert seen == [(0, 10), (3, 10), (6, 10), (9, 10), (10, 10)]
    assert len(res.null_columns) == 5


def test_bad_column_is_isolated_not_fatal():
    client = FakeClient({T.ref: (SCHEMA, ROWS)}, bad_columns={"addr.city"})
    res = make_scanner(client).scan(T)
    assert res.status == "ok"
    assert [c.name for c in res.error_columns] == ["addr.city"]
    assert "Syntax error" in res.error_columns[0].error
    assert len(res.null_columns) == 5


def test_missing_table_is_reported():
    res = make_scanner(FakeClient({})).scan(T)
    assert res.status == "error"
    assert res.error.startswith("Not found")


def test_cancel():
    ev = threading.Event()
    ev.set()
    res = make_scanner(FakeClient({T.ref: (SCHEMA, ROWS)}), cancel_event=ev).scan(T)
    assert res.status == "cancelled"


def test_empty_table_all_null():
    client = FakeClient({T.ref: (SCHEMA, [])})
    res = make_scanner(client).scan(T)
    assert res.total_rows == 0
    assert len(res.null_columns) == 10


@pytest.mark.parametrize("text,expected", [
    ("p.d.t", ("p", "d", "t")),
    ("`p.d.t`", ("p", "d", "t")),
    ("p:d.t", ("p", "d", "t")),
    ("example.com:p.d.t", ("example.com:p", "d", "t")),
    ("d.t", ("dp", "d", "t")),
    ("t", ("dp", "dd", "t")),
])
def test_parse(text, expected):
    t = TableTarget.parse(text, default_project="dp", default_dataset="dd")
    assert (t.project, t.dataset, t.table) == expected


def test_parse_rejects():
    with pytest.raises(ValueError):
        TableTarget.parse("just_a_table")
