# Null Column Identifier

A desktop app that finds the columns in BigQuery tables that are **completely empty**: not a
single row has a value in them.

![Main window after a scan](assets/screenshot.png)

## Using it

1. **Add tables** in one of these ways:
   - **Browse BigQuery…**: pick projects, search, and tick datasets or single tables (see below).
   - **Add tables…** or **Ctrl+V**: paste names, one per line (`project.dataset.table`, or just
     `table` with a default project and dataset).
   - **Import list…**, or drag a file onto the list: an Excel or CSV file. The old
     `Table_name_credentials.xlsx` (sheet *Credentials* with `CredentialsAccountJson`,
     `Project_id`, `Dataset_id`, `table_id`) works as is.
2. **Choose how to sign in**:
   - *My Google account*: run `gcloud auth application-default login` once on the computer.
   - *Service account key file*: pick the `.json` key.

   A key file set on a single row (right-click → *Set key file*) overrides this, so tables
   from different projects can use different keys.
3. Optionally set a **safety limit**, so tables that are too big are skipped instead of
   billed, and choose how many **tables to scan at the same time** (default 4).
4. Click **Run scan** (F5). Only the selected rows are scanned, or the rows shown by the list
   filter, or else all rows. Progress shows per table, and **Cancel** stops the running
   queries. The window stays responsive the whole time.
5. When it finishes, the **Results** tab lists every table and its empty columns, with a
   filter and *Copy null columns*. The Excel report opens automatically.

The table list and all settings are remembered between sessions.

## Finding tables in big organisations

![Browse BigQuery](assets/browse.png)

Browse BigQuery has three panes: **projects**, **datasets & tables** of the current project, and
the tables you've **selected**.

- **Projects**: type to filter. Recently used projects sit at the top, and each shows how
  many tables you've ticked in it. A project that isn't listed can be typed in and opened
  with Enter.
- **Search covers the whole project**: typing in the table search lists every dataset in the
  background, in parallel, so matches in datasets you haven't opened still show up. Use
  several words, which must all match, or `*` wildcards: `orders 2024`, `sales.*_2024`.
- **Tick** a table, a whole dataset (even before it has loaded), or **Tick all shown** (Ctrl+A)
  for everything the search matches. Selections stay when you switch projects, so you can
  collect tables from several projects in one go. Remove them on the right.
- **Keyboard**: Ctrl+F jumps to the table search, ↓ moves into the list, Space ticks.
- **Listings are cached on disk**, so reopening is instant even for projects with tens of
  thousands of tables. ↻ refreshes them from BigQuery. Tables already in your list are
  marked "in list".

## The report

`NullColumns_<date_time>.xlsx` in the chosen folder:

| Sheet | Contents |
|---|---|
| **Overview** | One row per table: rows, columns, null columns, coverage %, data scanned, status, link to details. Tables with no null columns are listed too (100% coverage). |
| **Null Columns** | Every empty column across all tables, ready to filter. |
| *one sheet per table* | Every column with its type, non-null row count, % filled, and result. Empty columns are highlighted. |
| **Run Info** | Who ran it, when, how long it took, totals and the definition used. |

A `.log` file of the run is saved next to the report.

## How "null" is decided

- A normal column is null when `COUNTIF(col IS NOT NULL) = 0`.
- An array column (`REPEATED`) is null when every row holds an empty array. BigQuery never
  stores a NULL array, so `IS NOT NULL` would always be true for arrays.
- A field inside a record (`addr.city`) is checked with its full path.
- A field inside a repeated record, at any depth (`items.parts.serial`), is null when no
  element in any row has a value.
- A table with 0 rows reports every column as null. The report says so.

Each table is read **once**: one query counts every column at the same time, and several
tables run in parallel. That costs the
same as `SELECT *` on the table. Very wide tables are split into batches of 400 columns. If
BigQuery rejects a batch, it is split in half until the problem column is found. That column
is reported as *Not checked* and the rest of the table still gets scanned.

## Running from source

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python app.py                   # or: python -m nci
```

## Building the Windows app

Double-click `build_windows.bat`, or run it from a Command Prompt. It creates a virtual
environment, runs the tests, builds with PyInstaller and checks the result. Share the whole
`dist\NullColumnIdentifier` folder (zip it). Expect roughly 75–90 MB on disk, about 35 MB zipped.
The build only bundles the Qt parts the app uses and leaves out pandas, numpy and grpc. Users only run `NullColumnIdentifier.exe` and do
not need Python.

## Project layout

```
app.py                 entry point
nci/core.py            schema flattening, SQL, scanning (no UI code)
nci/report.py          Excel report
nci/catalog.py         project/dataset/table listings with disk cache
nci/inputs.py          import/export of table lists
nci/gui/               PySide6 desktop interface
tests/                 tests, with a fake BigQuery client
```

Run the tests with `pip install -r requirements-dev.txt` and then `pytest`.
