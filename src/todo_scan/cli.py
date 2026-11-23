"""Command-line interface for todo-scan.

Exit codes (a public contract, keep them stable after the first release):

    0  scan finished cleanly and no --fail-on tag was found
    1  a --fail-on tag was found (takes precedence over 2: the gate is
       definitely failed, whatever else went wrong)
    2  the scan could not be trusted: bad arguments, a path that does not exist
       or cannot be read, an incomplete scan (unreadable directories, git failed
       inside a repo), a failure to write the output, or an internal error

Any other code would also mean "do not trust the result". Code 1 is reserved for
"a tag was found", so nothing else may ever end the process with 1: an uncaught
exception would do exactly that, which is why main() converts them to 2.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from . import __version__
from .scanner import DEFAULT_MAX_SIZE, DEFAULT_TAGS, Finding, ScanResult, scan_paths, validate_tags

EXIT_OK = 0
EXIT_FOUND = 1
EXIT_ERROR = 2
MAX_WARNINGS_SHOWN = 10


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="todo-scan",
        description="Find TODO/FIXME-style comments in files and directories.",
        epilog=(
            "Exit codes: 0 clean; 1 a --fail-on tag was found; "
            "2 usage error, incomplete scan or other failure (see stderr). "
            "Any code other than 0 and 1 means the result cannot be trusted."
        ),
    )
    parser.add_argument(
        "paths", nargs="*", default=["."], metavar="PATH",
        help="files or directories to scan (default: current directory)",
    )
    parser.add_argument(
        "--tags", metavar="TAGS",
        help="comma-separated UPPERCASE tags (default: " + ",".join(DEFAULT_TAGS) + ")",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text", help="output format")
    parser.add_argument(
        "--fail-on", metavar="TAGS",
        help="exit with code 1 if any of these tags is found; 'all' means every scanned tag",
    )
    parser.add_argument(
        "--exclude", action="append", default=[], metavar="GLOB",
        help="skip paths where the whole path or any component matches this glob (repeatable)",
    )
    parser.add_argument(
        "--no-git", action="store_true",
        help="do not ask git for the file list; walk the directory and skip common build/vendor folders",
    )
    parser.add_argument(
        "--max-size", type=_positive_int, default=DEFAULT_MAX_SIZE, metavar="BYTES",
        help="skip files larger than this (default: %(default)s)",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="do not print the summary line (warnings are always printed)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _prepare_streams() -> None:
    """Make printing robust on consoles whose encoding cannot show every character."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


def _format_finding(finding: Finding) -> str:
    label = finding.tag if finding.author is None else f"{finding.tag}({finding.author})"
    head = f"{finding.path}:{finding.line}: {label}"
    return f"{head}: {finding.message}" if finding.message else head


def _summary_line(result: ScanResult) -> str:
    total = len(result.findings)
    files_with_findings = len({f.path for f in result.findings})
    if total:
        by_tag = ", ".join(f"{tag}: {count}" for tag, count in sorted(Counter(f.tag for f in result.findings).items()))
        line = f"Found {total} in {files_with_findings} file(s) ({by_tag}); scanned {result.files_scanned} file(s)"
    else:
        line = f"No matches; scanned {result.files_scanned} file(s)"
    if result.skipped:
        details = ", ".join(f"{reason}: {count}" for reason, count in sorted(result.skipped.items()))
        line += f"; skipped {sum(result.skipped.values())} ({details})"
    if result.warnings:
        line += f"; {len(result.warnings)} warning(s), scan incomplete"
    return line


def _print_warnings(warnings: Sequence[str]) -> None:
    for text in warnings[:MAX_WARNINGS_SHOWN]:
        print(f"todo-scan: warning: {text}", file=sys.stderr)
    hidden = len(warnings) - MAX_WARNINGS_SHOWN
    if hidden > 0:
        print(f"todo-scan: warning: ... and {hidden} more", file=sys.stderr)


def _json_report(result: ScanResult) -> str:
    report = {
        "findings": [asdict(f) for f in result.findings],
        "warnings": list(result.warnings),
        "summary": {
            "total": len(result.findings),
            "by_tag": dict(sorted(Counter(f.tag for f in result.findings).items())),
            "files_scanned": result.files_scanned,
            "skipped": dict(sorted(result.skipped.items())),
            "complete": not result.warnings,
        },
    }
    # ensure_ascii=True keeps the output valid on any console or pipe encoding.
    return json.dumps(report, indent=2, ensure_ascii=True)


def _silence_broken_pipe() -> None:
    """Called after `todo-scan | head`: stop Python from complaining at exit."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, sys.stdout.fileno())
        finally:
            os.close(devnull)
    except (OSError, ValueError):
        pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    _prepare_streams()
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        tags = validate_tags(_split_csv(args.tags)) if args.tags is not None else DEFAULT_TAGS
    except ValueError as error:
        parser.error(str(error))

    fail_tags: set[str] = set()
    if args.fail_on is not None:
        requested = _split_csv(args.fail_on)
        if not requested:
            parser.error("--fail-on needs at least one tag or 'all'")
        if "all" in requested:
            fail_tags = set(tags)
        else:
            unknown = [t for t in requested if t not in tags]
            if unknown:
                parser.error(f"--fail-on tag(s) not among scanned tags: {', '.join(unknown)}")
            fail_tags = set(requested)

    try:
        return _run(args, tags, fail_tags)
    except Exception:  # noqa: BLE001 - see the module docstring: a crash must not look like exit code 1
        traceback.print_exc()
        print("todo-scan: error: unexpected internal error, the result cannot be trusted", file=sys.stderr)
        return EXIT_ERROR


def _run(args: argparse.Namespace, tags: Sequence[str], fail_tags: set[str]) -> int:
    try:
        result = scan_paths(
            [Path(p) for p in args.paths],
            tags,
            use_git=not args.no_git,
            excludes=args.exclude,
            max_size=args.max_size,
        )
    except OSError as error:  # FileNotFoundError, PermissionError, symlink loop, ...
        print(f"todo-scan: error: {error}", file=sys.stderr)
        return EXIT_ERROR

    try:
        if args.format == "json":
            print(_json_report(result))
        else:
            for finding in result.findings:
                print(_format_finding(finding))
        sys.stdout.flush()
    except BrokenPipeError:
        # The reader left early (`todo-scan | head -n1`). That says nothing about the
        # scan, so fall through and still report the real exit code.
        _silence_broken_pipe()
    except OSError as error:
        print(f"todo-scan: error: cannot write the report: {error}", file=sys.stderr)
        return EXIT_ERROR

    _print_warnings(result.warnings)
    if not args.quiet and args.format == "text":
        print(_summary_line(result), file=sys.stderr)

    if any(f.tag in fail_tags for f in result.findings):
        return EXIT_FOUND
    if result.warnings:
        return EXIT_ERROR
    return EXIT_OK
