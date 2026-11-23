from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from todo_scan import cli as cli_module
from todo_scan.cli import main
from todo_scan.scanner import Finding, ScanResult

# The directory the imported package really lives in, so subprocess tests run the same code.
SRC = Path(cli_module.__file__).resolve().parents[1]


def run(capsys, *args: str):
    code = main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture()
def project(tmp_path):
    (tmp_path / "a.py").write_text("# TODO: fix\n# FIXME: crash\n", encoding="utf-8")
    (tmp_path / "b.js").write_text("// TODO(ann): later\n", encoding="utf-8")
    return tmp_path


def test_text_output_and_default_exit_code(project, capsys):
    code, out, err = run(capsys, str(project), "--no-git")
    assert code == 0
    assert "a.py:1: TODO: fix" in out
    assert "a.py:2: FIXME: crash" in out
    assert "b.js:1: TODO(ann): later" in out
    assert "Found 3 in 2 file(s)" in err


def test_fail_on_matching_tag(project, capsys):
    assert run(capsys, str(project), "--no-git", "--fail-on", "FIXME")[0] == 1


def test_fail_on_tag_not_present_in_results(project, capsys):
    assert run(capsys, str(project), "--no-git", "--tags", "TODO,BUG", "--fail-on", "BUG")[0] == 0


def test_fail_on_all(project, capsys):
    assert run(capsys, str(project), "--no-git", "--fail-on", "all")[0] == 1


def test_no_findings_exit_zero_even_with_fail_on_all(tmp_path, capsys):
    (tmp_path / "a.py").write_text("print('hi')\n")
    code, out, err = run(capsys, str(tmp_path), "--no-git", "--fail-on", "all")
    assert code == 0
    assert out == ""
    assert "No matches" in err


def test_missing_path_exit_two(tmp_path, capsys):
    code, out, err = run(capsys, str(tmp_path / "missing"))
    assert code == 2
    assert "path not found" in err


@pytest.mark.parametrize(
    "args",
    [
        ["--tags", "todo"], ["--tags", ""],
        ["--fail-on", "NOPE"], ["--fail-on", ""],
        ["--max-size", "0"], ["--max-size", "abc"],
    ],
)
def test_bad_arguments_exit_two(project, args):
    with pytest.raises(SystemExit) as excinfo:
        main([str(project), "--no-git", *args])
    assert excinfo.value.code == 2


def test_json_output(project, capsys):
    code, out, err = run(capsys, str(project), "--no-git", "--format", "json")
    report = json.loads(out)
    assert code == 0
    assert report["summary"]["total"] == 3
    assert report["summary"]["by_tag"] == {"FIXME": 1, "TODO": 2}
    assert {"path", "line", "tag", "author", "message"} <= set(report["findings"][0])
    assert err == ""  # summary line is text-mode only


def test_json_is_ascii_safe_for_cyrillic(tmp_path, capsys):
    (tmp_path / "a.py").write_text("# TODO: исправить\n", encoding="utf-8")
    _, out, _ = run(capsys, str(tmp_path), "--no-git", "--format", "json")
    assert out.isascii()
    assert json.loads(out)["findings"][0]["message"] == "исправить"


def test_quiet_suppresses_summary(project, capsys):
    _, _, err = run(capsys, str(project), "--no-git", "-q")
    assert err == ""


def test_exclude_option(project, capsys):
    _, out, _ = run(capsys, str(project), "--no-git", "--exclude", "*.js")
    assert "b.js" not in out and "a.py" in out


def test_default_path_is_current_directory(project, capsys, monkeypatch):
    monkeypatch.chdir(project)
    code, out, _ = run(capsys, "--no-git")
    assert code == 0 and "a.py:1: TODO: fix" in out


def test_summary_reports_skipped_files(tmp_path, capsys):
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01")
    (tmp_path / "a.py").write_text("# TODO: x\n")
    _, _, err = run(capsys, str(tmp_path), "--no-git")
    assert "skipped 1 (binary: 1)" in err


# --- exit-code contract: 0 clean, 1 gate tag found, 2 usage error or incomplete scan ---


def _stub_scan(monkeypatch, result: ScanResult) -> None:
    monkeypatch.setattr(cli_module, "scan_paths", lambda *args, **kwargs: result)


def _finding(tag: str) -> Finding:
    return Finding("a.py", 1, tag, None, "x")


def test_incomplete_scan_exits_two(monkeypatch, capsys):
    _stub_scan(monkeypatch, ScanResult(warnings=["cannot read directory /x: denied"]))
    code, out, err = run(capsys, ".")
    assert code == 2
    assert out == ""
    assert "todo-scan: warning: cannot read directory /x: denied" in err
    assert "scan incomplete" in err


def test_incomplete_scan_with_findings_but_no_gate_still_exits_two(monkeypatch, capsys):
    _stub_scan(monkeypatch, ScanResult(findings=[_finding("TODO")], warnings=["w"]))
    assert run(capsys, ".")[0] == 2


def test_gate_failure_takes_precedence_over_incomplete_scan(monkeypatch, capsys):
    _stub_scan(monkeypatch, ScanResult(findings=[_finding("FIXME")], warnings=["w"]))
    code, out, err = run(capsys, ".", "--fail-on", "FIXME")
    assert code == 1
    assert "warning: w" in err  # the incompleteness is still visible


def test_clean_complete_scan_exits_zero_with_fail_on_set(monkeypatch, capsys):
    _stub_scan(monkeypatch, ScanResult(findings=[_finding("TODO")]))
    assert run(capsys, ".", "--fail-on", "FIXME")[0] == 0


def test_warnings_are_printed_even_with_quiet(monkeypatch, capsys):
    _stub_scan(monkeypatch, ScanResult(warnings=["w"]))
    code, _, err = run(capsys, ".", "-q")
    assert code == 2
    assert "warning: w" in err
    assert "scan incomplete" not in err  # only the summary line is quiet


def test_json_reports_warnings_and_completeness(monkeypatch, capsys):
    _stub_scan(monkeypatch, ScanResult(warnings=["w1", "w2"]))
    code, out, _ = run(capsys, ".", "--format", "json")
    report = json.loads(out)
    assert code == 2
    assert report["warnings"] == ["w1", "w2"]
    assert report["summary"]["complete"] is False


def test_json_marks_a_clean_scan_complete(project, capsys):
    _, out, _ = run(capsys, str(project), "--no-git", "--format", "json")
    report = json.loads(out)
    assert report["warnings"] == []
    assert report["summary"]["complete"] is True


def test_long_warning_list_is_capped_on_screen_but_complete_in_json(monkeypatch, capsys):
    many = [f"w{i}" for i in range(25)]
    _stub_scan(monkeypatch, ScanResult(warnings=many))
    _, _, err = run(capsys, ".")
    assert err.count("todo-scan: warning:") == 11  # 10 shown + the "and 15 more" line
    assert "and 15 more" in err
    _stub_scan(monkeypatch, ScanResult(warnings=many))
    _, out, _ = run(capsys, ".", "--format", "json")
    assert json.loads(out)["warnings"] == many


def test_venv_without_gitignore_is_hidden_through_the_cli(tmp_path, capsys):
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "a.py").write_text("# TODO: venv\n")
    (tmp_path / "app.py").write_text("# TODO: mine\n")
    _, out, _ = run(capsys, str(tmp_path), "--no-git")
    assert "app.py" in out and ".venv" not in out


# --- exit codes when the reader of the pipe leaves, or the scan itself fails ----


class _BrokenStdout:
    """Stands in for a stdout that cannot be written: its reader left, or the disk is full."""

    def __init__(self, error: OSError) -> None:
        self.error = error

    def write(self, _text: str) -> int:
        raise self.error

    def flush(self) -> None:
        raise self.error

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno")


def _closed_pipe() -> _BrokenStdout:
    return _BrokenStdout(BrokenPipeError(32, "Broken pipe"))


@pytest.mark.parametrize("extra, expected", [(["--fail-on", "all"], 1), ([], 0)])
def test_closed_stdout_pipe_does_not_change_the_exit_code(project, monkeypatch, capsys, extra, expected):
    monkeypatch.setattr(sys, "stdout", _closed_pipe())
    assert main([str(project), "--no-git", *extra]) == expected
    assert "Traceback" not in capsys.readouterr().err


def test_closed_stdout_pipe_still_reports_warnings_and_exit_two(monkeypatch, capsys):
    _stub_scan(monkeypatch, ScanResult(findings=[_finding("TODO")], warnings=["w"]))
    monkeypatch.setattr(sys, "stdout", _closed_pipe())
    assert main(["."]) == 2
    assert "warning: w" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="closing a pipe early behaves differently on Windows")
@pytest.mark.parametrize(
    "extra, expected",
    [(["--fail-on", "all"], 1), ([], 0), (["--fail-on", "all", "--format", "json"], 1)],
)
def test_real_pipe_closed_early_keeps_the_exit_code(tmp_path, extra, expected):
    # Same as `todo-scan --fail-on all | head -n1`. 30000 findings overflow any pipe buffer,
    # so the process is still writing when the reader leaves.
    (tmp_path / "big.py").write_text("# TODO: item\n" * 30000)
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "todo_scan", str(tmp_path), "--no-git", *extra],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    assert proc.stdout is not None and proc.stderr is not None
    proc.stdout.read(10)
    proc.stdout.close()
    err = proc.stderr.read().decode()
    proc.wait(timeout=60)
    proc.stderr.close()
    assert proc.returncode == expected
    assert "Traceback" not in err and "Exception ignored" not in err


@pytest.mark.skipif(sys.platform == "win32", reason="os.pipe semantics differ on Windows")
def test_nothing_stays_buffered_for_the_exit_flush_after_a_closed_pipe(project, monkeypatch):
    # Small output sits in the buffer; the reader is already gone. Our own flush() hits
    # BrokenPipeError, and the buffer would still be full when the interpreter flushes at
    # exit, ending the process with code 120 and an "Exception ignored" message.
    read_end, write_end = os.pipe()
    os.close(read_end)
    stream = os.fdopen(write_end, "w")
    monkeypatch.setattr(sys, "stdout", stream)
    assert main([str(project), "--no-git", "--fail-on", "all"]) == 1
    stream.flush()  # what the interpreter does at exit; must not raise
    stream.close()


def test_failure_to_write_the_report_exits_two(project, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdout", _BrokenStdout(OSError(28, "No space left on device")))
    assert main([str(project), "--no-git"]) == 2
    assert "cannot write the report" in capsys.readouterr().err


def _raising_scan(monkeypatch, error: BaseException) -> None:
    def scan(*args, **kwargs):
        raise error

    monkeypatch.setattr(cli_module, "scan_paths", scan)


@pytest.mark.parametrize(
    "error",
    [
        PermissionError(13, "Permission denied", "/x"),
        FileNotFoundError("path not found: /x"),
        OSError(5, "Input/output error"),
    ],
)
def test_oserror_during_the_scan_exits_two(monkeypatch, capsys, error):
    _raising_scan(monkeypatch, error)
    code, out, err = run(capsys, ".")
    assert code == 2
    assert out == ""
    assert err.startswith("todo-scan: error: ") and "Traceback" not in err


def test_unexpected_exception_exits_two_never_one(monkeypatch, capsys):
    # An uncaught exception would end the process with 1, which means "a tag was found".
    _raising_scan(monkeypatch, RuntimeError("boom"))
    code, _, err = run(capsys, ".")
    assert code == 2
    assert "RuntimeError: boom" in err and "unexpected internal error" in err


def test_keyboard_interrupt_is_not_swallowed(monkeypatch):
    _raising_scan(monkeypatch, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        main(["."])


needs_unprivileged_posix = pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="permission bits are not enforced for root and do not exist on Windows",
)


@needs_unprivileged_posix
def test_path_below_an_unreadable_directory_is_a_permission_error_not_a_missing_path(tmp_path, capsys):
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "x.py").write_text("# TODO: x\n")
    locked.chmod(0o000)
    try:
        code, _, err = run(capsys, str(locked / "x.py"))
    finally:
        locked.chmod(0o700)
    assert code == 2
    assert "Permission denied" in err and "path not found" not in err


def test_python_dash_m_entry_point(project):
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    proc = subprocess.run(
        [sys.executable, "-m", "todo_scan", str(project), "--no-git", "--fail-on", "FIXME"],
        capture_output=True, text=True, env=env, check=False,
    )
    assert proc.returncode == 1
    assert "a.py:2: FIXME: crash" in proc.stdout
