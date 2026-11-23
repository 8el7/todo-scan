# todo-scan

[![tests](https://github.com/8el7/todo-scan/actions/workflows/ci.yml/badge.svg)](https://github.com/8el7/todo-scan/actions/workflows/ci.yml)

Find `TODO`, `FIXME`, `HACK`, `XXX` and `BUG` comments in a project and print them as `file:line` lines
(clickable in most editors and terminals), as JSON, or as a CI gate.

- **No dependencies.** Standard library only.
- **Respects `.gitignore`** by asking git for the file list (`git ls-files`), so nested ignore files and
  negation rules behave exactly as git does. Always skips `.venv`, `node_modules`, `__pycache__` and similar
  folders, even in a project that has no `.gitignore`. Without git it walks the directory instead, and
  it warns you if that fallback happens inside a repository.
- **A heuristic, tuned for ordinary code.** A tag counts only right after a comment opener, which keeps prose
  like `# Handle TODO items` out of the report. It is not a parser, so a few lookalikes still match; see
  [Known limitations](#known-limitations).
- **CI-friendly.** `--fail-on FIXME` makes the exit code non-zero when a chosen tag is present.

[Русская версия](https://github.com/8el7/todo-scan/blob/main/README.ru.md)

## Install

```bash
pipx install todo-scan      # or: pip install todo-scan
```

Python 3.9 or newer.

## Usage

```console
$ todo-scan app
app/main.py:2: TODO(ann): handle missing files
app/main.py:6: FIXME: not atomic, can corrupt the file on crash
app/util.js:1: HACK: remove once the API returns ISO dates
Found 3 in 2 file(s) (FIXME: 1, HACK: 1, TODO: 1); scanned 2 file(s)
```

Findings go to stdout, the summary line goes to stderr, so piping stays clean.

```bash
todo-scan                                   # current directory
todo-scan src tests/test_api.py             # several paths
todo-scan --tags TODO,NOTE                  # your own UPPERCASE tags
todo-scan --exclude "*.min.js" --exclude vendor
todo-scan --format json > todos.json
todo-scan --fail-on FIXME,BUG -q            # CI: fail if any FIXME or BUG exists
```

| Option | Meaning |
|---|---|
| `--tags TAGS` | Comma-separated tags. Default: `TODO,FIXME,HACK,XXX,BUG`. Tags are case-sensitive and UPPERCASE. |
| `--format text\|json` | Output format. JSON is ASCII-escaped so it is valid on any console encoding. |
| `--fail-on TAGS` | Exit with code 1 if any of these tags is found. `all` means every scanned tag. |
| `--exclude GLOB` | Skip a path if the whole relative path or any of its components matches. Repeatable. |
| `--no-git` | Do not use git; walk the directory and skip `.git`, `node_modules`, `venv`, `dist`, `build` and similar. |
| `--max-size BYTES` | Skip larger files (default 1,000,000). |
| `-q`, `--quiet` | No summary line. Warnings are still printed. |

## Exit codes

The meaning of `0`, `1` and `2` is fixed from 0.1.0. Any code other than `0` and `1` means the result cannot be
trusted. New codes may be added in the future, so treat an unknown code like `2`.

| Code | Meaning |
|---|---|
| `0` | The scan finished cleanly and no `--fail-on` tag was found. Findings alone never change the code. |
| `1` | A `--fail-on` tag was found. This wins over `2`: the gate has failed whatever else happened. |
| `2` | The result cannot be trusted: bad arguments, a path that does not exist or cannot be read, an **incomplete scan** (a directory or file that cannot be read, or git failing or complaining inside a repository), a failure to write the output, or an internal error. Details go to stderr as `todo-scan: warning: ...` (also with `-q`) and appear under `warnings` in JSON output, where `summary.complete` is `false`. |

A reader that closes the pipe early (`todo-scan --fail-on all | head -n1`) does not change the exit code.

Only folders *below* the path you pass are filtered, so `todo-scan --no-git .venv/lib` scans exactly what you
point it at. Without `--no-git`, git decides which files exist: if your `.gitignore` ignores the folder you pass
(it usually ignores `.venv`), git lists nothing and you get 0 scanned files and exit code 0. To scan an ignored
folder, use `--no-git`.

## What counts as a comment

A line matches when a comment opener (`#`, `//`, `/*`, `*`, `--`, `<!--`, `;`, `%`, `"""`, `'''`) is
followed directly by a tag, an optional `(author)`, and an optional `:` or `-`:

```
# TODO: fix this          // FIXME(ann): later          /* HACK: temporary */          <!-- BUG: x -->
```

## Known limitations

Known and planned for a later release, not bugs to report:

- It is a line-based heuristic, not a parser. Anything that looks like a comment opener followed by a tag is
  reported: a string literal (`s = "# TODO: x"`), a Markdown heading (`## TODO list`), or arithmetic, because
  `*` and `%` count as comment openers (`x = 2 * TODO`, `a % TODO`).
- A tag is found only on a line that itself has a comment opener before the tag. In a block comment, lines
  that start with `*` (the usual Javadoc style) are found. A continuation line without a leading `*` is not.
  A docstring line is found only if it has a comment opener of its own: the text right after the opening `"""`
  on the same line, or a line starting with `#`, `*` and so on. A bare `TODO:` on the next line is not found.
  Only the first tag on a line is found.
- Tags are UPPERCASE only: `todo:` is ignored.
- Files are decoded as UTF-8; invalid bytes are replaced instead of aborting the scan.
  UTF-16 files contain NUL bytes and count as binary.
- A folder that your `.gitignore` ignores gives an empty result even when you pass it explicitly. Use `--no-git`
  (see [Exit codes](#exit-codes)).
- Git warnings other than `could not open directory` are ignored. If a `.gitignore` file is unreadable, git warns
  and applies fewer rules, so you may see extra findings from files that should have been ignored. The scan
  is not marked incomplete, because no file is missing.

Skipped silently on purpose: symlinks, FIFOs and other special files, and folders such as `.venv` and
`node_modules`. Counted as skipped in the summary line, which is not an error: binary files and files over
`--max-size`.

Reported as warnings, with exit code 2 because the scan is incomplete:

- a directory or file that cannot be read, including a tracked file inside a directory you cannot enter;
- git's `could not open directory` warning: git prints it while still exiting with 0, and the files in that
  directory are missing from its list;
- a failed git call inside a repository. The tool still scans by walking the directory, but your `.gitignore`
  is **not** applied. The warning quotes git's own error, for example "dubious ownership".

## Similar tools

Others do this job too: [todo-tree](https://crates.io/crates/todo-tree) (Rust),
[todos](https://github.com/ianlewis/todos) (Go), [TodoScope](https://pypi.org/project/todoscope/) (Python 3.12+).
`todo-scan` aims at the small niche of a dependency-free Python tool with git-accurate file selection.

## Development

```bash
git clone https://github.com/8el7/todo-scan
cd todo-scan
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
ruff check .
mypy
```

## License

MIT
