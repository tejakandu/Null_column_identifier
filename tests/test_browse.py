import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from types import SimpleNamespace  # noqa: E402

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from nci.catalog import Catalog  # noqa: E402
from nci.core import TableTarget  # noqa: E402
from nci.gui.browse import BrowseDialog, make_matcher  # noqa: E402


class BigCatalogClient:
    """Many projects, datasets and tables; counts API calls."""

    def __init__(self, n_projects=400, n_datasets=300, n_tables=100):
        self.n_projects, self.n_datasets, self.n_tables = n_projects, n_datasets, n_tables
        self.calls = 0

    def list_projects(self):
        self.calls += 1
        return [SimpleNamespace(project_id=f"proj-{i:03d}") for i in range(self.n_projects)]

    def list_datasets(self, project):
        self.calls += 1
        return [SimpleNamespace(dataset_id=f"ds_{i:03d}") for i in range(self.n_datasets)]

    def list_tables(self, ref):
        self.calls += 1
        ds = ref.split(".")[1]
        return [SimpleNamespace(table_id=f"{ds}_tbl_{j:03d}" + ("_2024" if j % 10 == 0 else ""),
                                table_type="VIEW" if j % 7 == 0 else "TABLE")
                for j in range(self.n_tables)]


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def wait_until(app, cond, timeout=20.0):
    # Keep the event loop running like the real app does; sleeping between
    # passes would measure the OS scheduler on a busy machine, not the app.
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0)
    return False


def cpu_seconds() -> float:
    """CPU time of this process. Speed limits use it instead of wall-clock
    time: a slower app still fails them, a busy machine does not."""
    return time.process_time()


def make(app, tmp_path, client=None, **kw):
    client = client or BigCatalogClient()
    catalog = Catalog(lambda project: client, str(tmp_path / "cache"), "me")
    return BrowseDialog(catalog, **kw), client, catalog


def test_matcher():
    m = make_matcher("sales *_2024")
    assert m("sales.orders_2024") and not m("sales.orders_2023") and not m("hr.x_2024")
    assert make_matcher("   ") is None


def test_projects_listed_recent_first_and_filtered(app, tmp_path):
    dlg, _, _ = make(app, tmp_path, recent_projects=["proj-399", "custom-proj"])
    assert wait_until(app, lambda: dlg.project_model.rowCount() == 401)
    assert dlg.project_model.item(0).data(Qt.UserRole + 2) == "proj-399"
    dlg.project_search.setText("proj-05")
    dlg._filter_projects()
    assert dlg.project_proxy.rowCount() == 10


def test_search_covers_whole_project_quickly(app, tmp_path):
    dlg, client, _ = make(app, tmp_path, start_project="proj-001")
    assert wait_until(app, lambda: len(dlg._dataset_items) == 300)
    dlg.table_search.setText("ds_12 2024")
    dlg._apply_table_filter()
    started = cpu_seconds()
    assert wait_until(app, lambda: dlg._bulk is None and len(dlg._table_items) == 30_000, timeout=120)
    loaded_in = cpu_seconds() - started
    assert loaded_in < 10, loaded_in
    # ds_120..ds_129, each with 10 tables ending in _2024
    shown = sum(dlg.tree_proxy.rowCount(dlg.tree_proxy.index(r, 0)) for r in range(dlg.tree_proxy.rowCount()))
    assert shown == 100

    t0 = cpu_seconds()
    dlg._tick_shown(True)
    assert cpu_seconds() - t0 < 2
    assert len(dlg.selected) == 100
    assert dlg.selected_list.count() == 100
    assert dlg.add_btn.isEnabled() and dlg.add_btn.text() == "Add 100 tables"

    # Filtering again is instant (no new API calls) and selections survive.
    calls = client.calls
    t0 = cpu_seconds()
    dlg.table_search.setText("tbl_05")
    dlg._apply_table_filter()
    assert cpu_seconds() - t0 < 1.5
    assert client.calls == calls
    assert len(dlg.selected) == 100


def test_whole_dataset_tick_before_loading_and_cross_project(app, tmp_path):
    dlg, _, _ = make(app, tmp_path, start_project="proj-001")
    assert wait_until(app, lambda: len(dlg._dataset_items) == 300)
    ds = dlg._dataset_items["ds_007"]
    ds.setCheckState(Qt.Checked)  # not loaded yet -> loads, then ticks all
    assert wait_until(app, lambda: len(dlg.selected) == 100)
    assert dlg.add_btn.isEnabled()

    dlg.open_project("proj-002")
    assert wait_until(app, lambda: len(dlg._dataset_items) == 300 and dlg.project == "proj-002")
    item = dlg._dataset_items["ds_000"]
    dlg.tree.expand(dlg.tree_proxy.mapFromSource(item.index()))
    assert wait_until(app, lambda: dlg._is_loaded("ds_000"))
    item.child(3).setCheckState(Qt.Checked)
    assert len(dlg.selected) == 101
    assert item.checkState() == Qt.PartiallyChecked

    # Removing from the right-hand list unticks the tree.
    dlg.selected_list.item(dlg.selected_list.count() - 1).setSelected(True)
    dlg._remove_highlighted()
    assert len(dlg.selected) == 100
    assert item.child(3).checkState() == Qt.Unchecked

    dlg._accept()
    assert len(dlg.targets) == 100 and all(t.project == "proj-001" for t in dlg.targets)


def test_cache_makes_reopen_instant(app, tmp_path):
    client = BigCatalogClient(n_datasets=20, n_tables=10)
    dlg, _, catalog = make(app, tmp_path, client=client, start_project="proj-001")
    assert wait_until(app, lambda: len(dlg._dataset_items) == 20)
    dlg.table_search.setText("tbl")
    dlg._apply_table_filter()
    assert wait_until(app, lambda: dlg._bulk is None and len(dlg._table_items) == 200)
    dlg.close()
    calls = client.calls

    # New dialog + new Catalog object reading the same cache folder: no API calls.
    catalog2 = Catalog(lambda project: client, str(tmp_path / "cache"), "me")
    dlg2 = BrowseDialog(catalog2, start_project="proj-001")
    assert wait_until(app, lambda: len(dlg2._table_items) == 200)
    assert client.calls == calls


def test_existing_tables_marked(app, tmp_path):
    dlg, _, _ = make(app, tmp_path, start_project="proj-001",
                     existing=[TableTarget("proj-001", "ds_000", "ds_000_tbl_001")])
    assert wait_until(app, lambda: len(dlg._dataset_items) == 300)
    dlg._load_dataset("ds_000")
    assert wait_until(app, lambda: dlg._is_loaded("ds_000"))
    row = dlg._dataset_items["ds_000"]
    assert row.child(1, 1).text().endswith("in list")
