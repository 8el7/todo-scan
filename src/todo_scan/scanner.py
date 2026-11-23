"""Core logic of todo-scan: file discovery and comment extraction.

The scanner is a line-based heuristic, not a language parser. A tag counts
only when it directly follows a comment opener (``#``, ``//``, ``/*``, ``*``,
``--``, ``<!--``, ``;``, ``%``, triple quotes), so ordinary prose such as
"# Handle TODO items" is ignored. The price of not parsing is that text inside
a string literal that merely looks like a comment is reported as if it were one.

Anything that makes a scan incomplete (an unreadable directory or file, a
failed git call inside a repository, git's own "could not open directory"
warning) is recorded in ``ScanResult.warnings`` instead of being swallowed. Normal skips
(binary, too large) are not warnings.

File types are decided with one ``lstat`` call rather than ``Path.is_file()``:
on Python 3.12 ``is_file()`` raises PermissionError for a file inside a
directory you cannot enter, while on 3.14 it quietly returns False. Both
versions must end up with the same visible warning.
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat
import subprocess
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_TAGS = ("TODO", "FIXME", "HACK", "XXX", "BUG")
DEFAULT_MAX_SIZE = 1_000_000  # bytes; larger files are skipped
BINARY_SNIFF_BYTES = 8192
GIT_TIMEOUT_SECONDS = 60

# Directories nobody wants scanned. Applied in BOTH modes: a project without a
# .gitignore (a fresh IDE project, say) lists its .venv as "untracked, not
# ignored", so git alone cannot be trusted to hide it. Only directories *below*
# the scanned root are checked, so scanning ".venv/lib" explicitly still works.
ALWAYS_EXCLUDED_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
        ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    }
)
# Skipped only when we walk the directory ourselves. In git mode these are left
# to .gitignore, because a committed "build" or ".vscode" folder may be intentional.
FALLBACK_ONLY_EXCLUDED_DIRS = frozenset({".idea", ".vscode", "dist", "build"})

_TAG_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*")
_COMMENT_OPENERS = ("#", "//", "/*", "*", "--", "<!--", ";", "%", '"""', "'''")
_COMMENT_CLOSERS = ("*/", "-->", '"""', "'''")
_OPENER_PATTERN = "(?:" + "|".join(re.escape(o) for o in _COMMENT_OPENERS) + ")"
_TRAILING_CLOSER_RE = re.compile(
    r"\s*(?:" + "|".join(re.escape(c) for c in _COMMENT_CLOSERS) + r")\s*$"
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    tag: str
    author: Optional[str]
    message: str


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    files_scanned: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def validate_tags(tags: Iterable[str]) -> tuple[str, ...]:
    """Return tags without duplicates.

    Raises TypeError if ``tags`` is a single string (it would be silently split
    into one-letter tags) or holds non-strings, and ValueError for a malformed
    or empty tag list.
    """
    if isinstance(tags, (str, bytes)):
        raise TypeError("tags must be a sequence of strings, not a single string")
    cleaned: list[str] = []
    for raw in tags:
        if not isinstance(raw, str):
            raise TypeError(f"tags must be strings, got {type(raw).__name__}: {raw!r}")
        tag = raw.strip()
        if not _TAG_NAME_RE.fullmatch(tag):
            raise ValueError(
                f"invalid tag {raw!r}: use UPPERCASE letters, digits and "
                "underscores, starting with a letter"
            )
        if tag not in cleaned:
            cleaned.append(tag)
    if not cleaned:
        raise ValueError("at least one tag is required")
    return tuple(cleaned)


def build_pattern(tags: Sequence[str]) -> re.Pattern[str]:
    """Compile the regex that recognises ``<opener> TAG(author): message``."""
    alternation = "|".join(re.escape(t) for t in sorted(tags, key=len, reverse=True))
    return re.compile(
        _OPENER_PATTERN
        + r"[ \t]*@?(?P<tag>"
        + alternation
        + r")\b(?:\((?P<author>[^)]*)\))?[ \t]*[:\-]?[ \t]*(?P<message>.*)"
    )


def extract_findings(text: str, path: str, pattern: re.Pattern[str]) -> list[Finding]:
    """Find at most one tagged comment per line.

    Lines are split on "\\n" only. ``str.splitlines`` would also split on
    form feed and other separators, which shifts line numbers relative to
    what editors show.
    """
    findings: list[Finding] = []
    for number, line in enumerate(text.split("\n"), start=1):
        match = pattern.search(line)
        if match is None:
            continue
        message = _TRAILING_CLOSER_RE.sub("", match["message"]).strip()
        author = (match["author"] or "").strip() or None
        findings.append(Finding(path, number, match["tag"], author, message))
    return findings


def scan_file(
    path: Path,
    pattern: re.Pattern[str],
    display: Optional[str] = None,
    max_size: int = DEFAULT_MAX_SIZE,
) -> tuple[list[Finding], Optional[str]]:
    """Scan one file. Returns (findings, skip_reason).

    skip_reason is None, "too_large", "binary" or "unreadable". Undecodable
    bytes are replaced rather than raising, so one odd file cannot abort a
    whole scan. UTF-16 files contain NUL bytes and are treated as binary.
    """
    try:
        if path.stat().st_size > max_size:
            return [], "too_large"
        with path.open("rb") as handle:
            data = handle.read(max_size + 1)
    except OSError:
        return [], "unreadable"
    if len(data) > max_size:  # file grew between stat() and read()
        return [], "too_large"
    if b"\x00" in data[:BINARY_SNIFF_BYTES]:
        return [], "binary"
    text = data.decode("utf-8-sig", errors="replace")
    shown = display if display is not None else _display_path(path)
    return extract_findings(text, shown, pattern), None


def _display_path(path: Path) -> str:
    """Path relative to the current directory, or absolute if it lies outside it."""
    try:
        relative = Path(os.path.relpath(path))
    except ValueError:  # different drive on Windows
        return path.as_posix()
    if relative.parts and relative.parts[0] == "..":
        return Path(os.path.abspath(path)).as_posix()
    return relative.as_posix()


def _git_listed_files(root: Path, warnings: list[str]) -> tuple[Optional[list[Path]], str]:
    """Tracked plus untracked-but-not-ignored files.

    Returns (files, "") on success, or (None, reason) when git cannot say.
    Git can succeed (exit code 0) and still skip part of the tree: for an
    unreadable directory it prints "warning: could not open directory" to
    stderr and lists nothing from it. Those lines are added to ``warnings``;
    other git warnings are deliberately ignored (see the comment below).
    LC_ALL=C keeps the message in English whatever the user's locale.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except FileNotFoundError:
        return None, "git is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return None, f"git did not answer within {GIT_TIMEOUT_SECONDS} seconds"
    except (OSError, subprocess.SubprocessError) as error:
        return None, f"could not run git: {error}"
    if proc.returncode != 0:
        lines = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        detail = lines[0] if lines else f"exit code {proc.returncode}"
        return None, f"'git ls-files' failed: {detail}"
    for line in proc.stderr.decode("utf-8", errors="replace").splitlines():
        prefix, _, text = line.partition(":")
        # Only the warning that means files are MISSING from the list. Anything else git says
        # (an unreadable global ignore file, GIT_TRACE output, ...) leaves the list complete or
        # at worst adds files, and must not turn a good scan into exit code 2.
        if prefix == "warning" and text.strip().startswith("could not open directory"):
            warnings.append(f"{root}: git: {text.strip()}")
    return [root / os.fsdecode(name) for name in proc.stdout.split(b"\0") if name], ""


def _inside_git_repo(root: Path) -> bool:
    """True if root or any parent holds a .git entry (a directory, or a file for worktrees).

    Uses os.path.exists, which answers False on any OS error on every Python
    version, unlike Path.exists (raises PermissionError before 3.14).
    """
    try:
        start = root.resolve()
    except (OSError, RuntimeError):  # RuntimeError: symlink loop on older Pythons
        return False
    return any(os.path.exists(candidate / ".git") for candidate in (start, *start.parents))


def _walk_files(root: Path, warnings: list[str]) -> Iterator[Path]:
    def on_error(error: OSError) -> None:
        where = error.filename if error.filename is not None else root
        warnings.append(f"cannot read directory {where}: {error.strerror or error}")

    skip = ALWAYS_EXCLUDED_DIRS | FALLBACK_ONLY_EXCLUDED_DIRS
    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in skip)
        for name in sorted(filenames):
            yield Path(dirpath) / name


def _is_excluded(relative: str, patterns: Sequence[str]) -> bool:
    """True if a glob matches the whole relative path or any path component."""
    if not patterns:
        return False
    parts = relative.split("/")
    for pattern in patterns:
        if fnmatch.fnmatch(relative, pattern):
            return True
        if any(fnmatch.fnmatch(part, pattern) for part in parts):
            return True
    return False


def _iter_directory(
    root: Path, use_git: bool, excludes: Sequence[str], warnings: list[str]
) -> Iterator[Path]:
    candidates: Optional[Iterable[Path]] = None
    if use_git:
        candidates, reason = _git_listed_files(root, warnings)
        # Outside a repository a failing git is normal and needs no comment.
        # Inside one it means .gitignore is silently not honoured, so say so.
        if candidates is None and _inside_git_repo(root):
            warnings.append(f"{root}: {reason}; .gitignore is NOT applied")
    if candidates is None:
        candidates = _walk_files(root, warnings)
    for path in candidates:
        relative = path.relative_to(root).as_posix()
        if any(part in ALWAYS_EXCLUDED_DIRS for part in relative.split("/")[:-1]):
            continue
        if _is_excluded(relative, excludes):
            continue
        try:
            mode = path.lstat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            continue  # listed by git but deleted from disk
        except OSError as error:
            warnings.append(f"cannot access {_display_path(path)}: {error.strerror or error}")
            continue
        # Only regular files. Symlinks are skipped (following them risks cycles and
        # double counting); FIFOs and sockets would block on open(); directories
        # appear in git's list for submodules.
        if not stat.S_ISREG(mode):
            continue
        yield path


def _classify_root(root: Path) -> str:
    """Return "file", "dir" or "other" for a path given on the command line.

    Raises FileNotFoundError if it does not exist and any other OSError
    (PermissionError, symlink loop) as it is, so a path that exists but cannot
    be entered is never reported as missing.
    """
    try:
        mode = os.stat(root).st_mode
    except (FileNotFoundError, NotADirectoryError):
        raise FileNotFoundError(f"path not found: {root}") from None
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "dir"
    return "other"


def scan_paths(
    roots: Sequence[Path],
    tags: Sequence[str] = DEFAULT_TAGS,
    *,
    use_git: bool = True,
    excludes: Sequence[str] = (),
    max_size: int = DEFAULT_MAX_SIZE,
) -> ScanResult:
    """Scan files and directories.

    Raises FileNotFoundError for a missing root, another OSError (for example
    PermissionError) for a root that exists but cannot be examined, and
    TypeError/ValueError for bad tags. A root that is a file is always scanned
    (no exclude rules apply to it). Overlapping roots are deduplicated by
    absolute path. Problems that make the scan incomplete are collected in
    ``result.warnings``.
    """
    pattern = build_pattern(validate_tags(tags))
    result = ScanResult()
    seen: set[str] = set()
    for raw_root in roots:
        root = Path(raw_root)
        kind = _classify_root(root)
        if kind == "other":
            result.warnings.append(f"{root}: not a regular file or directory, skipped")
            continue
        if kind == "file":
            paths: Iterable[Path] = [root]
        else:
            paths = _iter_directory(root, use_git, excludes, result.warnings)
        for path in paths:
            key = os.path.abspath(path)
            if key in seen:
                continue
            seen.add(key)
            shown = _display_path(path)
            findings, skipped = scan_file(path, pattern, shown, max_size)
            if skipped == "unreadable":
                result.warnings.append(f"cannot read file {shown}")
                continue
            if skipped is not None:
                result.skipped[skipped] = result.skipped.get(skipped, 0) + 1
                continue
            result.files_scanned += 1
            result.findings.extend(findings)
    result.findings.sort(key=lambda f: (f.path, f.line))
    return result
