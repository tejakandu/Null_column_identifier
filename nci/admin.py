"""Admin tool: set up the tracking tables and publish releases.

    python -m nci.admin setup   --dataset my-admin-project.null_column_identifier
    python -m nci.admin release 2.3.0 --url "https://…/NullColumnIdentifier-2.3.0.zip" \\
                                --notes "Faster browse" --mandatory
    python -m nci.admin list
    python -m nci.admin withdraw 2.3.0
    python -m nci.admin sql     --dataset my-admin-project.null_column_identifier

Uses your gcloud login (or --key FILE). `setup` writes the table names into
nci/nci_config.json so the next build of the app picks them up.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

from . import __version__
from .config import FILE_NAME, load_config
from .core import make_client
from .releases import RELEASES_SCHEMA, fetch_releases, parse_version
from .tracking import RUN_LOG_SCHEMA, to_bigquery_schema

RUN_LOG = "run_log"
RELEASES = "app_releases"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), FILE_NAME)


def ddl(dataset: str) -> str:
    def cols(spec, indent="  "):
        lines = []
        for name, ftype, mode, desc, *sub in spec:
            if sub:
                inner = cols(sub[0], indent + "  ")
                t = f"STRUCT<\n{inner}\n{indent}>"
            else:
                t = {"FLOAT": "FLOAT64", "INTEGER": "INT64", "BOOLEAN": "BOOL"}.get(ftype, ftype)
            if mode == "REPEATED":
                t = f"ARRAY<{t}>"
            null = " NOT NULL" if mode == "REQUIRED" else ""
            opt = f" OPTIONS(description={json.dumps(desc)})" if desc else ""
            lines.append(f"{indent}`{name}` {t}{null}{opt}")
        return ",\n".join(lines)

    return (f"CREATE TABLE IF NOT EXISTS `{dataset}.{RUN_LOG}` (\n{cols(RUN_LOG_SCHEMA)}\n)\n"
            f"PARTITION BY DATE(execution_time)\nCLUSTER BY project, dataset, `table`;\n\n"
            f"CREATE TABLE IF NOT EXISTS `{dataset}.{RELEASES}` (\n{cols(RELEASES_SCHEMA)}\n);\n")


def _client(project: str, key: str):
    return make_client(project, key)


def cmd_setup(args) -> int:
    from google.api_core.exceptions import Conflict
    from google.cloud import bigquery

    project, dataset_id = _split_dataset(args.dataset)
    client = _client(project, args.key)
    existing = _find_dataset(client, project, dataset_id)
    if existing is not None:
        # Use the existing dataset as is (creating one needs project-level rights).
        dataset_id = existing.dataset_id  # exact spelling: dataset names are case-sensitive
        print(f"Using existing dataset {project}.{dataset_id} ({existing.location})")
    elif args.create_dataset:
        ds = bigquery.Dataset(f"{project}.{dataset_id}")
        ds.location = args.location
        ds.description = "Null Column Identifier: run tracking and app releases"
        client.create_dataset(ds)
        print(f"Created dataset {project}.{dataset_id} ({args.location})")
    else:
        print(f"Dataset {project}.{dataset_id} was not found (names are case-sensitive).\n"
              f"Check the name in the BigQuery console, or add --create-dataset to create it.",
              file=sys.stderr)
        return 1

    runs = bigquery.Table(f"{project}.{dataset_id}.{RUN_LOG}", schema=to_bigquery_schema(RUN_LOG_SCHEMA))
    runs.time_partitioning = bigquery.TimePartitioning(field="execution_time")
    runs.clustering_fields = ["project", "dataset", "table"]
    runs.description = "Null Column Identifier: one row per table per run"
    rel = bigquery.Table(f"{project}.{dataset_id}.{RELEASES}", schema=to_bigquery_schema(RELEASES_SCHEMA))
    rel.description = "Published versions of the Null Column Identifier app"
    for table in (runs, rel):
        name = f"{table.project}.{table.dataset_id}.{table.table_id}"
        try:
            client.create_table(table)
            print(f"Created {name}")
        except Conflict:
            print(f"Exists  {name}")

    _write_config({"run_log_table": f"{project}.{dataset_id}.{RUN_LOG}",
                   "releases_table": f"{project}.{dataset_id}.{RELEASES}"})
    print(f"\nWrote table names to {CONFIG_PATH}. Rebuild the app to ship them.")
    print("\nGive app users access (e.g. to a Google group) on this dataset:\n"
          f"  - write runs:    roles/bigquery.dataEditor on {RUN_LOG}\n"
          f"  - read releases: roles/bigquery.dataViewer on {RELEASES}\n"
          "  (or roles/bigquery.dataEditor on the whole dataset)")
    return 0


def _find_dataset(client, project: str, dataset_id: str):
    """The dataset, also when only the letter case differs; None if missing."""
    from google.api_core.exceptions import NotFound

    try:
        return client.get_dataset(f"{project}.{dataset_id}")
    except NotFound:
        pass
    try:
        matches = [d.dataset_id for d in client.list_datasets(project)
                   if d.dataset_id.lower() == dataset_id.lower()]
    except Exception:  # noqa: BLE001 - listing is only a convenience
        matches = []
    if len(matches) == 1:
        print(f"Note: the dataset is spelled {matches[0]!r}, using that.")
        return client.get_dataset(f"{project}.{matches[0]}")
    return None


def cmd_release(args) -> int:
    from google.cloud import bigquery

    table = _releases_table(args)
    current = fetch_releases(_client(table.split(".")[0], args.key), table)
    newest = max((r.key for r in current), default=(0,))
    if parse_version(args.version) <= newest and not args.force:
        print(f"Version {args.version} is not newer than the latest published version "
              f"({'.'.join(map(str, newest))}). Use --force to publish anyway.", file=sys.stderr)
        return 1
    url = args.url or load_config().download_url
    if not url:
        print("Give --url (where users download the new version).", file=sys.stderr)
        return 1
    client = _client(table.split(".")[0], args.key)
    # DML (not streaming) so the row is visible to the app immediately.
    sql = (f"INSERT INTO `{table}` (version, released_at, mandatory, download_url, release_notes, "
           f"released_by, withdrawn) VALUES (@v, CURRENT_TIMESTAMP(), @m, @u, @n, @by, FALSE)")
    params = [bigquery.ScalarQueryParameter("v", "STRING", args.version),
              bigquery.ScalarQueryParameter("m", "BOOL", args.mandatory),
              bigquery.ScalarQueryParameter("u", "STRING", url),
              bigquery.ScalarQueryParameter("n", "STRING", args.notes or ""),
              bigquery.ScalarQueryParameter("by", "STRING", args.by or getpass.getuser())]
    client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()
    kind = "MANDATORY - older versions will close" if args.mandatory else "optional"
    print(f"Published {args.version} ({kind})\n  {url}")
    return 0


def cmd_withdraw(args) -> int:
    from google.cloud import bigquery

    table = _releases_table(args)
    client = _client(table.split(".")[0], args.key)
    job = client.query(f"UPDATE `{table}` SET withdrawn = TRUE WHERE version = @v",
                       job_config=bigquery.QueryJobConfig(query_parameters=[
                           bigquery.ScalarQueryParameter("v", "STRING", args.version)]))
    job.result()
    print(f"Withdrew {args.version} ({job.num_dml_affected_rows or 0} row(s))")
    return 0


def cmd_list(args) -> int:
    table = _releases_table(args)
    releases = sorted(fetch_releases(_client(table.split(".")[0], args.key), table),
                      key=lambda r: r.key, reverse=True)
    if not releases:
        print("No releases published yet.")
    for r in releases:
        when = r.released_at.strftime("%Y-%m-%d %H:%M") if r.released_at else ""
        print(f"{r.version:<10} {'MANDATORY' if r.mandatory else 'optional ':<9}  {when}  "
              f"{r.released_by:<14} {r.download_url}")
        if r.release_notes:
            print(f"{'':10} {r.release_notes}")
    print(f"\nThis copy of the app is {__version__}.")
    return 0


def cmd_sql(args) -> int:
    print(ddl(args.dataset))
    return 0


def _split_dataset(text: str) -> tuple[str, str]:
    parts = text.strip().strip("`").split(".")
    if len(parts) != 2 or not all(parts):
        raise SystemExit("--dataset must be PROJECT.DATASET")
    return parts[0], parts[1]


def _releases_table(args) -> str:
    table = args.table or load_config().releases_table
    if table.count(".") != 2:
        raise SystemExit("No releases table: run `setup` first or pass --table PROJECT.DATASET.TABLE")
    return table


def _write_config(updates: dict) -> None:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    data.update(updates)
    with open(CONFIG_PATH, "w", encoding="utf-8", newline="\n") as fh:  # same bytes on Windows
        json.dump(data, fh, indent=2)
        fh.write("\n")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m nci.admin", description=__doc__.split("\n\n")[0])
    p.add_argument("--key", default="", help="service account key file (default: your gcloud login)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="create the tables in a dataset, and write nci_config.json")
    s.add_argument("--dataset", required=True, help="PROJECT.DATASET to create the tables in")
    s.add_argument("--create-dataset", action="store_true",
                   help="create the dataset if it does not exist (needs project-level rights)")
    s.add_argument("--location", default="US", help="location for a new dataset (default US)")
    s.set_defaults(fn=cmd_setup)

    r = sub.add_parser("release", help="publish a new version")
    r.add_argument("version")
    r.add_argument("--url", default="", help="download link for this version")
    r.add_argument("--notes", default="", help="what changed (shown to users)")
    r.add_argument("--mandatory", action="store_true", help="force older versions to update and close")
    r.add_argument("--by", default="", help="your name (default: login name)")
    r.add_argument("--force", action="store_true", help="publish even if not newer than the latest")
    r.add_argument("--table", default="")
    r.set_defaults(fn=cmd_release)

    w = sub.add_parser("withdraw", help="pull a published version")
    w.add_argument("version")
    w.add_argument("--table", default="")
    w.set_defaults(fn=cmd_withdraw)

    ls = sub.add_parser("list", help="show published versions")
    ls.add_argument("--table", default="")
    ls.set_defaults(fn=cmd_list)

    q = sub.add_parser("sql", help="print CREATE TABLE statements")
    q.add_argument("--dataset", required=True)
    q.set_defaults(fn=cmd_sql)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except Exception as exc:  # noqa: BLE001 - one readable line instead of a traceback
        from google.api_core.exceptions import GoogleAPICallError

        from .core import friendly_error

        if not isinstance(exc, (GoogleAPICallError, OSError)) and type(exc).__name__ != "DefaultCredentialsError":
            raise
        print(f"Error: {friendly_error(exc)}", file=sys.stderr)
        if "permission" in str(exc).lower() or "denied" in str(exc).lower():
            print("Ask the project owner for BigQuery Data Editor on the dataset "
                  "(and BigQuery Job User on the project to publish releases).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
