from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from todo_scan import scanner as scanner_module
from todo_scan.scanner import (
    DEFAULT_TAGS,
    build_pattern,
    extract_findings,
    scan_file,
    scan_paths,
    validate_tags,
)

PATTERN = build_pattern(DEFAULT_TAGS)


def find(text: str):
    return extract_findings(text, "f.txt", PATTERN)


def names(result) -> list[str]:
    return sorted(Path(f.path).name for f in result.findings)


# --- extract_findings -------------------------------------------------------


@pytest.mark.parametrize(
    "line, tag, message",
    [
        ("# TODO: a", "TODO", "a"),
        ("// FIXME: b", "FIXME", "b"),
        ("/* HACK: c */", "HACK", "c"),
        ("-- XXX: d", "XXX", "d"),
        ("<!-- BUG: e -->", "BUG", "e"),
        ("x = 1  # TODO fix it", "TODO", "fix it"),
        ("  * TODO: g", "TODO", "g"),
        ("; TODO: h", "TODO", "h"),
        ('""" TODO: doc """', "TODO", "doc"),
        ("# @TODO: at-sign", "TODO", "at-sign"),
        ("# TODO", "TODO", ""),
    ],
)
def test_comment_styles(line, tag, message):
    [finding] = find(line)
    assert (finding.tag, finding.message, finding.line) == (tag, message, 1)


def test_block_comment_lines_with_a_leading_star_are_found():
    # The usual Javadoc/C style: "*" counts as a comment opener.
    [finding] = find("/*\n * TODO: second line\n */")
    assert (finding.line, finding.message) == (2, "second line")


@pytest.mark.parametrize(
    "text",
    [
        "/* start\n   TODO: bare continuation line\n*/",  # no opener on the line itself
        '"""\nTODO: docstring body\n"""',
        '"""\n  FIXME: docstring body\n"""',
        '"""\n- TODO: dash bullet in a docstring\n"""',
        '"""\ntext TODO: tag in the middle of a docstring line\n"""',
        '"""\nTODO: closing quotes on the same line"""',
    ],
)
def test_lines_without_their_own_comment_opener_are_not_found(text):
    assert find(text) == []


@pytest.mark.parametrize(
    "text, line",
    [
        ('"""TODO: right after the opening quotes', 1),
        ("'''TODO: single quotes, same line", 1),
        ('def f():\n    """\n    # TODO: hash line inside a docstring\n    """', 3),
        ('"""\n * TODO: star line inside a docstring\n"""', 2),
    ],
)
def test_docstring_line_is_found_only_with_a_comment_opener_of_its_own(text, line):
    # The README states exactly this rule: the text right after the opening quotes
    # on the same line, or a line starting with "#" or "*". Not the next line.
    [finding] = find(text)
    assert finding.line == line


def test_only_the_first_tag_on_a_line_is_found():
    [finding] = find("x = 1  # TODO: first FIXME: second")
    assert (finding.tag, finding.message) == ("TODO", "first FIXME: second")


def test_author_is_parsed():
    [finding] = find("# TODO(Ivan): call the API")
    assert finding.author == "Ivan"
    assert finding.message == "call the API"


def test_empty_author_is_none():
    [finding] = find("# TODO(): x")
    assert finding.author is None


@pytest.mark.parametrize(
    "line",
    [
        "# Handle TODO items later",   # tag is not right after the opener
        "x = 'TODOS'",                 # no comment opener
        "# TODOS: plural",             # word boundary
        "# todo: lowercase",           # tags are case-sensitive
        "TODO: no comment marker",
        "",
    ],
)
def test_non_matches(line):
    assert find(line) == []


def test_line_numbers_ignore_crlf_and_form_feed():
    # str.splitlines() would split on \x0c and shift the line number to 4.
    [finding] = find("a\r\nb\x0c\n# TODO: x\r\n")
    assert finding.line == 3
    assert finding.message == "x"  # no stray \r


def test_custom_tags():
    pattern = build_pattern(("NOTE",))
    assert extract_findings("# NOTE: n\n# TODO: t", "f", pattern)[0].tag == "NOTE"
    assert len(extract_findings("# NOTE: n\n# TODO: t", "f", pattern)) == 1


def test_validate_tags():
    assert validate_tags(["TODO", " FIXME ", "TODO"]) == ("TODO", "FIXME")
    for bad in (["todo"], [""], ["TO DO"], ["1TODO"], []):
        with pytest.raises(ValueError):
            validate_tags(bad)


# --- scan_file --------------------------------------------------------------


def test_binary_file_is_skipped(tmp_path):
    path = tmp_path / "blob.bin"
    path.write_bytes(b"\x00# TODO: hidden")
    assert scan_file(path, PATTERN) == ([], "binary")


def test_large_file_is_skipped(tmp_path):
    path = tmp_path / "big.txt"
    path.write_text("# TODO: x\n" * 10)
    assert scan_file(path, PATTERN, max_size=10) == ([], "too_large")


def test_invalid_utf8_does_not_crash(tmp_path):
    path = tmp_path / "latin.txt"
    path.write_bytes(b"# TODO: \xff\xfe bad bytes\n")
    findings, skipped = scan_file(path, PATTERN)
    assert skipped is None
    assert "bad bytes" in findings[0].message


def test_utf8_bom_is_handled(tmp_path):
    path = tmp_path / "bom.txt"
    path.write_bytes(b"\xef\xbb\xbf# TODO: x\n")
    findings, _ = scan_file(path, PATTERN)
    assert [f.message for f in findings] == ["x"]


def test_cyrillic_message(tmp_path):
    path = tmp_path / "ru.py"
    path.write_text("# TODO: исправить\n", encoding="utf-8")
    findings, _ = scan_file(path, PATTERN)
    assert findings[0].message == "исправить"


def test_empty_file(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_bytes(b"")
    assert scan_file(path, PATTERN) == ([], None)


def test_missing_file_is_unreadable(tmp_path):
    assert scan_file(tmp_path / "nope.txt", PATTERN) == ([], "unreadable")


# --- scan_paths -------------------------------------------------------------


def test_fallback_walk_skips_vendor_dirs(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "src" / "a.py").write_text("# TODO: keep\n")
    (tmp_path / "node_modules" / "x.js").write_text("// TODO: skip\n")
    result = scan_paths([tmp_path], use_git=False)
    assert names(result) == ["a.py"]
    assert result.files_scanned == 1


def test_exclude_glob(tmp_path):
    (tmp_path / "a.js").write_text("// TODO: keep\n")
    (tmp_path / "a.min.js").write_text("// TODO: skip\n")
    result = scan_paths([tmp_path], use_git=False, excludes=["*.min.js"])
    assert names(result) == ["a.js"]


def test_explicit_file_ignores_excludes(tmp_path):
    folder = tmp_path / "node_modules"
    folder.mkdir()
    path = folder / "x.js"
    path.write_text("// TODO: explicit\n")
    assert names(scan_paths([path], use_git=False)) == ["x.js"]


def test_overlapping_roots_are_deduplicated(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("# TODO: once\n")
    result = scan_paths([tmp_path, path], use_git=False)
    assert len(result.findings) == 1


def test_missing_root_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        scan_paths([tmp_path / "missing"])


def test_symlinks_are_not_followed(tmp_path):
    real = tmp_path / "real.py"
    real.write_text("# TODO: once\n")
    try:
        os.symlink(real, tmp_path / "link.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    assert names(scan_paths([tmp_path], use_git=False)) == ["real.py"]


def test_display_path_relative_inside_cwd_absolute_outside(tmp_path, monkeypatch):
    inside = tmp_path / "work"
    outside = tmp_path / "elsewhere"
    inside.mkdir()
    outside.mkdir()
    (inside / "a.py").write_text("# TODO: in\n")
    (outside / "b.py").write_text("# TODO: out\n")
    monkeypatch.chdir(inside)
    shown = {Path(f.path).name: f.path for f in scan_paths([inside, outside], use_git=False).findings}
    assert shown["a.py"] == "a.py"
    assert shown["b.py"] == (outside / "b.py").as_posix()


# --- exclusions that must also apply in git mode ----------------------------

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@needs_git
def test_git_mode_skips_venv_and_node_modules_without_gitignore(tmp_path):
    # A project with no .gitignore: git lists .venv as "untracked, not ignored".
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "a.py").write_text("# TODO: venv\n")
    (tmp_path / "node_modules" / "pkg" / "b.js").write_text("// TODO: vendored\n")
    (tmp_path / "app.py").write_text("# TODO: mine\n")
    assert names(scan_paths([tmp_path], use_git=True)) == ["app.py"]


@needs_git
def test_git_mode_leaves_build_and_vscode_to_gitignore(tmp_path):
    # These are excluded only in walk mode; in git mode a committed folder is intentional.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "build").mkdir()
    (tmp_path / ".vscode").mkdir()
    (tmp_path / "build" / "gen.py").write_text("# TODO: generated\n")
    (tmp_path / ".vscode" / "task.py").write_text("# TODO: editor\n")
    assert names(scan_paths([tmp_path], use_git=True)) == ["gen.py", "task.py"]
    assert names(scan_paths([tmp_path], use_git=False)) == []


@pytest.mark.parametrize("use_git", [False, pytest.param(True, marks=needs_git)])
def test_explicit_root_inside_excluded_dir_is_scanned(tmp_path, use_git):
    lib = tmp_path / ".venv" / "lib"
    lib.mkdir(parents=True)
    (lib / "a.py").write_text("# TODO: asked for explicitly\n")
    if use_git:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert names(scan_paths([lib], use_git=use_git)) == ["a.py"]


# --- tags passed in the wrong shape -----------------------------------------


def test_tags_as_plain_string_is_rejected(tmp_path):
    # Used to be silently split into ('T', 'O', 'D', 'O').
    with pytest.raises(TypeError):
        validate_tags("TODO")
    with pytest.raises(TypeError):
        validate_tags(b"TODO")
    with pytest.raises(TypeError):
        validate_tags([1])
    with pytest.raises(TypeError):
        scan_paths([tmp_path], "TODO", use_git=False)


def test_tags_accept_any_iterable_of_strings():
    assert validate_tags(iter(["TODO", "FIXME"])) == ("TODO", "FIXME")


# --- warnings: an incomplete scan must never be silent ----------------------


def _no_git(*args, **kwargs):
    raise FileNotFoundError("git")


def test_warning_when_git_is_missing_inside_a_repository(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()  # looks like a repository; no real git needed
    (tmp_path / "a.py").write_text("# TODO: x\n")
    monkeypatch.setattr(scanner_module.subprocess, "run", _no_git)
    result = scan_paths([tmp_path], use_git=True)
    assert names(result) == ["a.py"]  # the fallback walk still produces results
    assert len(result.warnings) == 1
    assert "not installed" in result.warnings[0]
    assert "NOT applied" in result.warnings[0]


def test_warning_carries_the_reason_git_gave(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "a.py").write_text("# TODO: x\n")
    refused = subprocess.CompletedProcess(
        args=[], returncode=128, stdout=b"",
        stderr=b"fatal: detected dubious ownership in repository\nhint: second line\n",
    )
    monkeypatch.setattr(scanner_module.subprocess, "run", lambda *a, **k: refused)
    result = scan_paths([tmp_path], use_git=True)
    assert "dubious ownership" in result.warnings[0]
    assert "second line" not in result.warnings[0]  # only the first stderr line


def test_no_warning_when_git_fails_outside_a_repository(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("# TODO: x\n")
    monkeypatch.setattr(scanner_module.subprocess, "run", _no_git)
    monkeypatch.setattr(scanner_module, "_inside_git_repo", lambda root: False)
    assert scan_paths([tmp_path], use_git=True).warnings == []


def test_no_warning_with_no_git_flag_even_inside_a_repository(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "a.py").write_text("# TODO: x\n")
    monkeypatch.setattr(scanner_module.subprocess, "run", _no_git)
    assert scan_paths([tmp_path], use_git=False).warnings == []


def test_inside_git_repo_checks_parents_and_accepts_a_git_file(tmp_path):
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    (tmp_path / ".git").write_text("gitdir: /elsewhere\n")  # worktrees and submodules use a file
    assert scanner_module._inside_git_repo(deep) is True


def test_walk_error_becomes_a_warning(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("# TODO: x\n")

    def fake_walk(top, topdown=True, onerror=None, followlinks=False):
        onerror(PermissionError(13, "Permission denied", str(Path(top) / "locked")))
        yield str(top), [], ["a.py"]

    monkeypatch.setattr(os, "walk", fake_walk)
    result = scan_paths([tmp_path], use_git=False)
    assert names(result) == ["a.py"]  # scanning continues past the bad directory
    assert len(result.warnings) == 1
    assert "locked" in result.warnings[0] and "Permission denied" in result.warnings[0]


def test_unreadable_file_is_a_warning_not_a_quiet_skip(tmp_path, monkeypatch):
    (tmp_path / "ok.py").write_text("# TODO: ok\n")
    (tmp_path / "locked.py").write_text("# TODO: hidden\n")
    real = scanner_module.scan_file

    def flaky(path, *args, **kwargs):
        if path.name == "locked.py":
            return [], "unreadable"
        return real(path, *args, **kwargs)

    monkeypatch.setattr(scanner_module, "scan_file", flaky)
    result = scan_paths([tmp_path], use_git=False)
    assert names(result) == ["ok.py"]
    assert any("locked.py" in w for w in result.warnings)
    assert "unreadable" not in result.skipped


def test_binary_and_large_files_are_skips_not_warnings(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01")
    (tmp_path / "big.txt").write_text("x" * 50)
    result = scan_paths([tmp_path], use_git=False, max_size=10)
    assert result.warnings == []
    assert result.skipped == {"binary": 1, "too_large": 1}


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="permission bits are not enforced for root and do not exist on Windows",
)
def test_real_permission_denied_directory_is_reported(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "x.py").write_text("# TODO: unreachable\n")
    (tmp_path / "a.py").write_text("# TODO: reachable\n")
    locked.chmod(0o000)
    try:
        result = scan_paths([tmp_path], use_git=False)
    finally:
        locked.chmod(0o700)
    assert names(result) == ["a.py"]
    assert any("locked" in w for w in result.warnings)


# --- git succeeds but skips part of the tree --------------------------------

needs_unprivileged_posix = pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="permission bits are not enforced for root and do not exist on Windows",
)


def _git_answers(stdout: bytes, stderr: bytes = b"", returncode: int = 0):
    completed = subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)
    return lambda *args, **kwargs: completed


def test_git_warnings_on_stderr_are_forwarded_even_when_git_exits_zero(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("# TODO: x\n")
    stderr = b"warning: could not open directory 'locked/': Permission denied\n"
    monkeypatch.setattr(scanner_module.subprocess, "run", _git_answers(b"a.py\0", stderr))
    result = scan_paths([tmp_path], use_git=True)
    assert names(result) == ["a.py"]  # what git did list is still scanned
    assert result.warnings == [f"{tmp_path}: git: could not open directory 'locked/': Permission denied"]


def test_every_unreadable_directory_git_reports_gets_its_own_warning(tmp_path, monkeypatch):
    stderr = (
        b"warning: could not open directory 'a/': Permission denied\n"
        b"warning: could not open directory 'b/c/': Permission denied\n"
    )
    monkeypatch.setattr(scanner_module.subprocess, "run", _git_answers(b"", stderr))
    assert scan_paths([tmp_path], use_git=True).warnings == [
        f"{tmp_path}: git: could not open directory 'a/': Permission denied",
        f"{tmp_path}: git: could not open directory 'b/c/': Permission denied",
    ]


@pytest.mark.parametrize(
    "stderr",
    [
        # Real git 2.43 output when ~/.config/git/ignore or a repository .gitignore is unreadable:
        # the file list is complete (or has extra files), so the scan is not "incomplete".
        b"warning: unable to access '/home/u/.config/git/ignore': Permission denied\n",
        b"warning: unable to access '.gitignore': Permission denied\n",
        b"error: unable to read x\n",
        b"warning: something git may say in a future version\n",
        b"fatal: not a message we should ever forward on success\n",
    ],
)
def test_other_git_warnings_do_not_make_the_scan_incomplete(tmp_path, monkeypatch, stderr):
    monkeypatch.setattr(scanner_module.subprocess, "run", _git_answers(b"", stderr))
    assert scan_paths([tmp_path], use_git=True).warnings == []


@needs_git
@needs_unprivileged_posix
def test_real_unreadable_global_ignore_file_gives_no_warning(tmp_path, monkeypatch):
    config = tmp_path / "config"
    (config / "git").mkdir(parents=True)
    ignore = config / "git" / "ignore"
    ignore.write_text("*.log\n")
    ignore.chmod(0o000)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.py").write_text("# TODO: x\n")
    try:
        result = scan_paths([repo], use_git=True)
    finally:
        ignore.chmod(0o600)
    assert names(result) == ["a.py"]
    assert result.warnings == []


def test_git_debug_output_is_not_a_warning(tmp_path, monkeypatch):
    # GIT_TRACE and friends write to stderr; a user with them set must not get exit code 2.
    noise = b"12:00:00.1 git.c:460 trace: built-in: git ls-files\nhint: something helpful\n"
    monkeypatch.setattr(scanner_module.subprocess, "run", _git_answers(b"", noise))
    assert scan_paths([tmp_path], use_git=True).warnings == []


def test_git_is_run_with_the_c_locale(tmp_path, monkeypatch):
    seen: dict = {}

    def recorder(*args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(scanner_module.subprocess, "run", recorder)
    scan_paths([tmp_path], use_git=True)
    assert seen["env"]["LC_ALL"] == "C"  # keeps the "warning:" prefix recognisable


@needs_git
@needs_unprivileged_posix
def test_real_untracked_unreadable_directory_in_git_mode_is_reported(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "y.py").write_text("# TODO: hidden\n")
    (tmp_path / "a.py").write_text("# TODO: visible\n")
    locked.chmod(0o000)
    try:
        result = scan_paths([tmp_path], use_git=True)
    finally:
        locked.chmod(0o700)
    assert names(result) == ["a.py"]
    assert any("git:" in w and "locked" in w for w in result.warnings)


@needs_git
@needs_unprivileged_posix
def test_real_tracked_file_in_unreadable_directory_is_reported(tmp_path):
    # Python 3.12 raised PermissionError here, 3.14 skipped the file silently.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "x.py").write_text("# TODO: hidden\n")
    (tmp_path / "a.py").write_text("# TODO: visible\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "locked/x.py", "a.py"], check=True)
    locked.chmod(0o000)
    try:
        result = scan_paths([tmp_path], use_git=True)
    finally:
        locked.chmod(0o700)
    assert names(result) == ["a.py"]
    assert any("cannot access" in w and "x.py" in w for w in result.warnings)


def test_lstat_permission_error_becomes_a_warning_on_every_python_version(tmp_path, monkeypatch):
    (tmp_path / "x.py").write_text("# TODO: hidden\n")
    (tmp_path / "ok.py").write_text("# TODO: ok\n")
    monkeypatch.setattr(scanner_module.subprocess, "run", _git_answers(b"x.py\0ok.py\0"))
    real_lstat = Path.lstat

    def lstat(self):
        if self.name == "x.py":
            raise PermissionError(13, "Permission denied", str(self))
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", lstat)
    result = scan_paths([tmp_path], use_git=True)
    assert names(result) == ["ok.py"]
    assert len(result.warnings) == 1
    assert "cannot access" in result.warnings[0] and "x.py" in result.warnings[0]
    assert "Permission denied" in result.warnings[0]


def test_git_listed_path_below_a_file_is_skipped_silently(tmp_path, monkeypatch):
    # "sub" used to be a directory and is a file now: same as a deleted file, not a problem.
    (tmp_path / "sub").write_text("now a file\n")
    monkeypatch.setattr(scanner_module.subprocess, "run", _git_answers(b"sub/f.py\0"))
    result = scan_paths([tmp_path], use_git=True)
    assert (result.findings, result.warnings, result.files_scanned) == ([], [], 0)


def test_git_listed_directory_entry_is_not_scanned(tmp_path, monkeypatch):
    # git lists a submodule as a single path that is a directory on disk.
    (tmp_path / "submodule").mkdir()
    monkeypatch.setattr(scanner_module.subprocess, "run", _git_answers(b"submodule\0"))
    result = scan_paths([tmp_path], use_git=True)
    assert (result.files_scanned, result.warnings) == (0, [])


# --- roots given on the command line ----------------------------------------


def test_root_that_exists_but_cannot_be_examined_is_not_reported_as_missing(tmp_path, monkeypatch):
    target = tmp_path / "hidden"
    real_stat = os.stat

    def stat_(path, *args, **kwargs):
        if os.fspath(path) == str(target):
            raise PermissionError(13, "Permission denied", str(target))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat_)
    with pytest.raises(PermissionError):  # Python 3.14's Path.exists() would have said "False"
        scan_paths([target], use_git=False)


def test_root_below_a_regular_file_is_reported_as_missing(tmp_path):
    (tmp_path / "f.txt").write_text("x")
    with pytest.raises(FileNotFoundError, match="path not found"):
        scan_paths([tmp_path / "f.txt" / "inner"], use_git=False)


def test_symlink_loop_as_root_raises_oserror_but_not_filenotfound(tmp_path):
    loop = tmp_path / "loop"
    try:
        os.symlink(loop, loop)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    with pytest.raises(OSError) as excinfo:
        scan_paths([loop], use_git=False)
    assert not isinstance(excinfo.value, FileNotFoundError)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_fifo_as_root_is_a_warning_and_does_not_block(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    result = scan_paths([fifo], use_git=False)  # opening a FIFO would block forever
    assert (result.findings, result.files_scanned) == ([], 0)
    assert any("not a regular file or directory" in w for w in result.warnings)


def test_inside_git_repo_ignores_permission_errors_from_path_exists(tmp_path, monkeypatch):
    # Path.exists() raises PermissionError before Python 3.14 and returns False after it.
    (tmp_path / ".git").mkdir()

    def exists(self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "exists", exists)
    assert scanner_module._inside_git_repo(tmp_path) is True


def test_inside_git_repo_survives_resolve_errors(tmp_path, monkeypatch):
    def broken(self, strict=False):
        raise RuntimeError("Symlink loop")

    monkeypatch.setattr(Path, "resolve", broken)
    assert scanner_module._inside_git_repo(tmp_path) is False


def test_results_are_sorted(tmp_path):
    (tmp_path / "b.py").write_text("# TODO: 1\n# FIXME: 2\n")
    (tmp_path / "a.py").write_text("\n\n# TODO: 3\n")
    result = scan_paths([tmp_path], use_git=False)
    assert [(Path(f.path).name, f.line) for f in result.findings] == [("a.py", 3), ("b.py", 1), ("b.py", 2)]


def test_results_are_sorted_across_roots_given_in_reverse_order(tmp_path):
    first, second = tmp_path / "a_dir", tmp_path / "b_dir"
    first.mkdir()
    second.mkdir()
    (first / "x.py").write_text("# TODO: 1\n")
    (second / "x.py").write_text("# TODO: 2\n")
    result = scan_paths([second, first], use_git=False)
    assert [f.message for f in result.findings] == ["1", "2"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_mode_honours_gitignore(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("ignored/\n*.log\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "x.py").write_text("# TODO: ignored by git\n")
    (tmp_path / "debug.log").write_text("# TODO: ignored by git\n")
    (tmp_path / "keep.py").write_text("# TODO: untracked but not ignored\n")
    result = scan_paths([tmp_path], use_git=True)
    assert names(result) == ["keep.py"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_mode_in_subdirectory_lists_only_that_subtree(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "in.py").write_text("# TODO: in\n")
    (tmp_path / "out.py").write_text("# TODO: out\n")
    assert names(scan_paths([tmp_path / "sub"], use_git=True)) == ["in.py"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_deleted_tracked_file_is_skipped(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    gone = tmp_path / "gone.py"
    gone.write_text("# TODO: x\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "gone.py"], check=True)
    gone.unlink()
    assert scan_paths([tmp_path], use_git=True).findings == []


def test_non_git_directory_falls_back_to_walk(tmp_path):
    (tmp_path / "a.py").write_text("# TODO: x\n")
    # use_git=True in a directory that is not a repository must still work
    # (unless tmp_path happens to sit inside a repository; both paths find a.py).
    assert names(scan_paths([tmp_path], use_git=True)) == ["a.py"]
