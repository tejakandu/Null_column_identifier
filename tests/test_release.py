"""The one-command release: questions, plan, steps, safety checks, version stamping."""

import json
import os
import stat
import sys
import textwrap

import pytest

import nci
from nci import release
from tests.fakes import FakeClient


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"releases_table": "dp-qa-derived-e0f9.qa_automation.app_releases",
                               "run_log_table": "dp-qa-derived-e0f9.qa_automation.run_log"}))
    monkeypatch.setenv("NCI_CONFIG", str(cfg))
    monkeypatch.setattr(release, "LOCAL_SETTINGS", str(tmp_path / ".nci_release.json"))
    monkeypatch.setattr(release, "KEEP_DIR", str(tmp_path / "releases"))
    exe = tmp_path / "dist" / "NullColumnIdentifier.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ fake exe")
    calls = []
    steps = {
        "tests": lambda: calls.append(("tests",)),
        "build": lambda v: calls.append(("build", v)) or str(exe),
        "check": lambda p, v: calls.append(("check", p, v)) or {"ok": True, "version": v, "message": "OK"},
        "upload": lambda p, v, b, pre, key: calls.append(("upload", p, v, b, pre)) or f"https://dl/{v}",
        "register": lambda a, t, v, u: calls.append(("register", a.mandatory, a.notes, t, v, u)),
    }
    client = FakeClient({}, releases=[{"version": "2.2.1"}, {"version": "2.1.0"}])
    return {"tmp": tmp_path, "calls": calls, "steps": steps, "client": client, "exe": exe}


def args(*argv):
    import argparse

    ns = None

    def capture(parsed):
        nonlocal ns
        ns = parsed
        return 0

    p_run = release.run
    try:
        release.run = lambda a, **kw: capture(a)
        release.main(list(argv))
    finally:
        release.run = p_run
    assert isinstance(ns, argparse.Namespace)
    return ns


def answers(*items):
    it = iter(items)
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return next(it)

    ask.asked = asked
    return ask


def go(env, ask, *argv):
    return release.run(args(*argv), ask=ask, steps=env["steps"], client_for=lambda project: env["client"])


# --------------------------------------------------------------------------- versions


@pytest.mark.parametrize("published,base,expected", [
    ([], "2.2.1", "2.2.1"),                  # first release: the code's version
    (["2.2.1"], "2.2.1", "2.2.2"),
    (["2.2.1", "2.2.9", "2.1.0"], "2.2.1", "2.2.10"),
    (["2.2.1"], "3.0", "3.0.0"),             # base raised by hand for a big release
    (["2.10"], "2.2.1", "2.10.1"),
])
def test_next_version(published, base, expected):
    assert release.next_version(published, base) == expected


def test_version_is_stamped_into_builds(tmp_path, monkeypatch):
    stamp = tmp_path / "_build_version.txt"
    monkeypatch.setattr(nci, "BUILD_VERSION_FILE", str(stamp))
    assert nci._read_version() == nci.BASE_VERSION
    stamp.write_text("2.7.3\n")
    assert nci._read_version() == "2.7.3"


# --------------------------------------------------------------------------- happy path


def test_interactive_release_does_everything_in_order(env, capsys):
    ask = answers("gs://qa-tools/nci", "", "Faster browse", "y", "")
    assert go(env, ask) == 0
    assert [c[0] for c in env["calls"]] == ["tests", "build", "check", "upload", "register"]
    assert env["calls"][1] == ("build", "2.2.2")                      # suggested next version
    assert env["calls"][3][2:] == ("2.2.2", "qa-tools", "nci")
    assert env["calls"][4] == ("register", True, "Faster browse",
                               "dp-qa-derived-e0f9.qa_automation.app_releases", "2.2.2", "https://dl/2.2.2")
    assert (env["tmp"] / "releases" / "NullColumnIdentifier-2.2.2.exe").read_bytes() == b"MZ fake exe"
    out = capsys.readouterr().out
    assert "Latest published version: 2.2.1" in out and "MANDATORY" in out and "Released 2.2.2" in out
    # The bucket is remembered on this computer...
    assert json.loads((env["tmp"] / ".nci_release.json").read_text()) == {"bucket": "gs://qa-tools/nci"}
    # ...so the next release asks only three questions.
    env["client"].releases.append({"version": "2.2.2"})
    env["calls"].clear()
    ask2 = answers("", "Bug fixes", "", "")
    assert go(env, ask2) == 0
    assert len(ask2.asked) == 4 and "Cloud Storage" not in " ".join(ask2.asked)
    assert env["calls"][1] == ("build", "2.2.3") and env["calls"][4][1] is False


def test_no_questions_with_flags(env):
    ask = answers()  # any question would raise StopIteration
    assert go(env, ask, "--version", "2.5.0", "--notes", "x", "--optional", "--yes",
              "--bucket", "gs://b/f", "--skip-tests") == 0
    assert [c[0] for c in env["calls"]] == ["build", "check", "upload", "register"]
    assert env["calls"][0] == ("build", "2.5.0") and env["calls"][3][1] is False


def test_dry_run_publishes_nothing(env, capsys):
    assert go(env, answers(), "--dry-run", "--yes", "--notes", "", "--optional", "--bucket", "gs://b") == 0
    assert [c[0] for c in env["calls"]] == ["tests", "build", "check"]
    assert "Nothing was uploaded or published" in capsys.readouterr().out


# --------------------------------------------------------------------------- refusals


@pytest.mark.parametrize("version,why", [("2.2.1", "not newer"), ("2.0.9", "not newer"), ("v2.x", "not a version")])
def test_refuses_bad_versions_before_doing_anything(env, version, why):
    with pytest.raises(release.ReleaseError, match=why):
        go(env, answers(), "--version", version, "--bucket", "gs://b", "--notes", "", "--optional")
    assert env["calls"] == []


def test_declining_the_plan_releases_nothing(env):
    assert go(env, answers("n"), "--bucket", "gs://b", "--version", "3.0.0", "--notes", "", "--optional") == 1
    assert env["calls"] == []


def test_failed_step_stops_before_upload(env):
    def broken_check(path, version):
        raise release.ReleaseError("self-test failed")

    env["steps"]["check"] = broken_check
    with pytest.raises(release.ReleaseError):
        go(env, answers(), "--yes", "--bucket", "gs://b", "--notes", "", "--optional")
    assert [c[0] for c in env["calls"]] == ["tests", "build"]


def test_needs_releases_table(env, tmp_path, monkeypatch):
    (tmp_path / "empty.json").write_text("{}")
    monkeypatch.setenv("NCI_CONFIG", str(tmp_path / "empty.json"))
    monkeypatch.setattr("nci.config.candidate_paths", lambda: [str(tmp_path / "empty.json")])
    with pytest.raises(release.ReleaseError, match="nci.admin setup"):
        go(env, answers(), "--yes")


def test_main_prints_one_line_and_exits_1(env, monkeypatch, capsys):
    monkeypatch.setattr(release, "make_plan", lambda *a, **k: (_ for _ in ()).throw(
        release.ReleaseError("not newer than 2.2.1")))
    assert release.main(["--yes"]) == 1
    assert "Release stopped: not newer than 2.2.1" in capsys.readouterr().err


# --------------------------------------------------------------------------- real steps


def test_build_stamps_version_and_always_cleans_up(tmp_path, monkeypatch):
    stamp = tmp_path / "_build_version.txt"
    monkeypatch.setattr(release, "BUILD_VERSION_FILE", str(stamp))
    monkeypatch.setattr(release, "BUILT_EXE", str(tmp_path / "out.exe"))
    seen = {}

    def fake_run(cmd, cwd=None):
        seen["cmd"], seen["stamp"] = cmd, stamp.read_text()
        (tmp_path / "out.exe").write_text("x")
        return type("P", (), {"returncode": 0})()

    monkeypatch.setattr(release.subprocess, "run", fake_run)
    assert release.build("2.4.0") == str(tmp_path / "out.exe")
    assert seen["stamp"] == "2.4.0" and "PyInstaller" in seen["cmd"] and not stamp.exists()

    monkeypatch.setattr(release.subprocess, "run", lambda cmd, cwd=None: type("P", (), {"returncode": 1})())
    with pytest.raises(release.ReleaseError, match="build failed"):
        release.build("2.4.0")
    assert not stamp.exists()


def _fake_app(tmp_path, version, ok=True, code=0):
    """A stand-in .exe: a script that behaves like `app --selftest --report FILE`."""
    script = tmp_path / "fakeapp"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, sys
        report = sys.argv[sys.argv.index("--report") + 1]
        json.dump({{"ok": {ok}, "version": "{version}", "message": "fake OK"}}, open(report, "w"))
        sys.exit({code})
    """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.mark.skipif(os.name == "nt", reason="uses a shebang script as the stand-in app")
def test_check_build_runs_the_app_and_verifies_version(tmp_path):
    assert release.check_build(_fake_app(tmp_path, "2.4.0"), "2.4.0")["message"] == "fake OK"
    with pytest.raises(release.ReleaseError, match="reports version '2.3.0'"):
        release.check_build(_fake_app(tmp_path, "2.3.0"), "2.4.0")
    with pytest.raises(release.ReleaseError, match="self-test"):
        release.check_build(_fake_app(tmp_path, "2.4.0", ok=False), "2.4.0")
    with pytest.raises(release.ReleaseError, match="self-test"):
        release.check_build(_fake_app(tmp_path, "2.4.0", code=3), "2.4.0")


def test_selftest_report_file(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from nci.gui import app

    report = tmp_path / "r.json"
    monkeypatch.setattr(sys, "argv", ["app", "--selftest", "--report", str(report)])
    assert app.selftest() == 0
    data = json.loads(report.read_text())
    assert data["ok"] is True and data["version"] == nci.__version__


def test_yes_takes_defaults_without_asking(env):
    assert go(env, answers(), "--yes", "--bucket", "gs://b") == 0
    assert env["calls"][1] == ("build", "2.2.2")
    assert env["calls"][-1][1:3] == (False, "")   # optional, no notes
