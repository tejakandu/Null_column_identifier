"""One-command release: ask three questions, then test, build, check, upload, publish.

    release.bat                       (or: python -m nci.release)
    release.bat --dry-run             build and check, publish nothing
    release.bat --version 2.4.0 --notes "..." --mandatory --yes   (no questions)

The version is not edited in code: the tool suggests the next version after
the latest one in the releases table and stamps it into the build. The
Cloud Storage folder is asked once and remembered on this computer
(.nci_release.json, not committed).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Callable

from . import BASE_VERSION, BUILD_VERSION_FILE
from .config import load_config
from .releases import fetch_releases, parse_version

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_SETTINGS = os.path.join(ROOT, ".nci_release.json")
SPEC = os.path.join(ROOT, "NullColumnIdentifier.spec")
BUILT_EXE = os.path.join(ROOT, "dist", "NullColumnIdentifier.exe" if os.name == "nt" else "NullColumnIdentifier")
KEEP_DIR = os.path.join(ROOT, "releases")
STEPS = 5


class ReleaseError(Exception):
    """A step failed; the message says what to do."""


# ----------------------------------------------------------------------------- versions


def next_version(published: list[str], base: str = BASE_VERSION) -> str:
    """Suggested version: one patch after the newest published one.

    If the code's BASE_VERSION is ahead (raised by hand for a bigger
    release), that is suggested instead; the first release uses it too.
    """
    base_key = parse_version(base)
    newest = max((parse_version(v) for v in published), default=None)
    if newest is None or base_key > newest:
        return _fmt(base_key)
    parts = (list(newest) + [0, 0, 0])[:3]
    parts[2] += 1
    return _fmt(tuple(parts))


def _fmt(key: tuple[int, ...]) -> str:
    return ".".join(str(p) for p in (list(key) + [0, 0, 0])[:3])


def valid_version(text: str) -> bool:
    parts = text.split(".")
    return 1 <= len(parts) <= 4 and all(p.isdigit() for p in parts)


# ----------------------------------------------------------------------------- local settings


def load_local() -> dict:
    try:
        with open(LOCAL_SETTINGS, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_local(data: dict) -> None:
    with open(LOCAL_SETTINGS, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


# ----------------------------------------------------------------------------- steps


def run_tests() -> None:
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests"], cwd=ROOT)
    if proc.returncode != 0:
        raise ReleaseError("Tests failed (see above). Fix them, or use --skip-tests if you are sure.")


def build(version: str) -> str:
    """Build the .exe with the version stamped in; returns its path."""
    with open(BUILD_VERSION_FILE, "w", encoding="utf-8") as fh:
        fh.write(version)
    try:
        proc = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                               "--log-level", "WARN", SPEC], cwd=ROOT)
    finally:
        os.remove(BUILD_VERSION_FILE)  # source runs go back to the base version
    if proc.returncode != 0 or not os.path.isfile(BUILT_EXE):
        raise ReleaseError("The build failed (see above). If it mentions 'Access is denied', "
                           "close the app and try again.")
    return BUILT_EXE


def check_build(path: str, version: str) -> dict:
    """Start the built app in self-test mode; it must report this exact version."""
    with tempfile.TemporaryDirectory() as tmp:
        report = os.path.join(tmp, "selftest.json")
        try:
            proc = subprocess.run([path, "--selftest", "--report", report], timeout=180)
        except subprocess.TimeoutExpired:
            raise ReleaseError("The built app did not finish its self-test within 3 minutes.") from None
        try:
            with open(report, encoding="utf-8") as fh:
                result = json.load(fh)
        except (OSError, ValueError):
            result = {}
    if proc.returncode != 0 or not result.get("ok"):
        raise ReleaseError("The built app failed its self-test, so nothing was published.")
    if result.get("version") != version:
        raise ReleaseError(f"The built app reports version {result.get('version')!r}, expected {version!r}.")
    return result


# ----------------------------------------------------------------------------- flow


@dataclass
class Plan:
    version: str
    notes: str
    mandatory: bool
    bucket: str
    prefix: str
    table: str
    latest: str


def ask_text(prompt: str, default: str = "", ask: Callable[[str], str] = input) -> str:
    answer = ask(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return answer or default


def ask_yes(prompt: str, default: bool, ask: Callable[[str], str] = input) -> bool:
    answer = ask(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not answer else answer in ("y", "yes")


def make_plan(args, ask: Callable[[str], str] = input, client_for=None) -> Plan:
    from .admin import _client, _split_gcs

    cfg = load_config()
    table = cfg.releases_table
    if table.count(".") != 2:
        raise ReleaseError("No releases table is set up. Run: python -m nci.admin setup --dataset PROJECT.DATASET")

    local = load_local()
    bucket_text = args.bucket or cfg.release_bucket or local.get("bucket", "")
    if not bucket_text:
        bucket_text = ask_text("Cloud Storage folder for releases, e.g. gs://my-bucket/null-column-identifier",
                               ask=ask)
    bucket, prefix = _split_gcs(bucket_text)
    if not bucket:
        raise ReleaseError("A Cloud Storage folder is needed (gs://BUCKET/FOLDER).")
    if local.get("bucket") != bucket_text:
        local["bucket"] = bucket_text
        save_local(local)

    client = (client_for or (lambda project: _client(project, args.key)))(table.split(".")[0])
    published = [r.version for r in fetch_releases(client, table)]
    latest = max(published, key=parse_version) if published else ""
    print(f"Latest published version: {latest or '(none yet)'}")

    suggested = next_version(published)
    # --yes: take the defaults for anything not given on the command line.
    version = args.version or (suggested if args.yes else ask_text("New version", suggested, ask))
    if not valid_version(version):
        raise ReleaseError(f"{version!r} is not a version number like 2.3.0.")
    if latest and parse_version(version) <= parse_version(latest):
        raise ReleaseError(f"{version} is not newer than the latest published version {latest}.")

    if args.notes is not None:
        notes = args.notes
    else:
        notes = "" if args.yes else ask_text("What changed? (shown to users)", ask=ask)
    if args.mandatory is not None:
        mandatory = args.mandatory
    else:
        mandatory = False if args.yes else ask_yes("Force everyone on older versions to update?", False, ask)
    return Plan(version, notes, mandatory, bucket, prefix, table, latest)


def summary(plan: Plan, dry_run: bool) -> str:
    where = f"gs://{plan.bucket}/{plan.prefix + '/' if plan.prefix else ''}NullColumnIdentifier-{plan.version}.exe"
    lines = [
        "",
        "Release plan",
        f"  Version      {plan.version}" + (f"   (latest is {plan.latest})" if plan.latest else ""),
        f"  Update       {'MANDATORY - older versions will close' if plan.mandatory else 'optional'}",
        f"  Notes        {plan.notes or '(none)'}",
        f"  Upload to    {where}",
        f"  Register in  {plan.table}",
    ]
    if dry_run:
        lines.append("  DRY RUN      build and check only; nothing is uploaded or published")
    return "\n".join(lines) + "\n"


def run(args, ask: Callable[[str], str] = input, steps=None, client_for=None) -> int:
    """The whole release. ``steps`` lets tests replace the slow/outward-facing parts."""
    from .admin import _insert_release, upload_build

    steps = {"tests": run_tests, "build": build, "check": check_build,
             "upload": upload_build, "register": _insert_release, **(steps or {})}
    plan = make_plan(args, ask, client_for)
    print(summary(plan, args.dry_run))
    if not args.yes and not ask_yes("Go ahead?", True, ask):
        print("Nothing was released.")
        return 1

    def step(n, text):
        print(f"\n[{n}/{STEPS}] {text}")

    step(1, "Running tests...")
    if args.skip_tests:
        print("      skipped (--skip-tests)")
    else:
        steps["tests"]()
    step(2, f"Building version {plan.version}...")
    exe = steps["build"](plan.version)
    step(3, "Checking the built app...")
    result = steps["check"](exe, plan.version)
    print(f"      {result.get('message', 'OK')}")

    os.makedirs(KEEP_DIR, exist_ok=True)
    kept = os.path.join(KEEP_DIR, f"NullColumnIdentifier-{plan.version}.exe")
    shutil.copy2(exe, kept)
    if args.dry_run:
        print(f"\nDry run finished. The build is in {kept}\nNothing was uploaded or published.")
        return 0

    step(4, "Uploading...")
    url = steps["upload"](exe, plan.version, plan.bucket, plan.prefix, args.key)
    step(5, "Publishing...")
    reg = argparse.Namespace(key=args.key, mandatory=plan.mandatory, notes=plan.notes, by=args.by)
    steps["register"](reg, plan.table, plan.version, url)
    print(f"\nReleased {plan.version}. "
          + ("Older versions will ask users to download it and close."
             if plan.mandatory else "Users see an 'Update' button within the hour.")
          + f"\n  Download link: {url}\n  A copy is kept in {kept}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="release.bat", description=__doc__.split("\n\n")[0])
    p.add_argument("--version", default="", help="version to release (default: next after the latest)")
    p.add_argument("--notes", default=None, help="what changed (shown to users)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--mandatory", dest="mandatory", action="store_true", default=None,
                   help="older versions must update and will close")
    g.add_argument("--optional", dest="mandatory", action="store_false", help="update is optional")
    p.add_argument("--bucket", default="", help="gs://BUCKET/FOLDER (remembered for next time)")
    p.add_argument("--key", default="", help="service account key file (default: your gcloud login)")
    p.add_argument("--by", default="", help="your name for the release record")
    p.add_argument("--dry-run", action="store_true", help="build and check, publish nothing")
    p.add_argument("--skip-tests", action="store_true", help="skip the test run")
    p.add_argument("--yes", "-y", action="store_true",
                   help="ask nothing: suggested version, optional update, no notes unless given")
    args = p.parse_args(argv)
    try:
        return run(args)
    except ReleaseError as exc:
        print(f"\nRelease stopped: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nRelease cancelled. Nothing was published.", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - Google errors as one readable line
        from google.api_core.exceptions import GoogleAPICallError

        from .core import friendly_error

        if not isinstance(exc, GoogleAPICallError) and type(exc).__name__ != "DefaultCredentialsError":
            raise
        print(f"\nRelease stopped: {friendly_error(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
