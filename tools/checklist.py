"""Print the reliability checklist items whose triggers match a diff.

Reads docs/reliability-checklist.md from the base (origin/main by default), not from the
working tree, and matches each item's
Paths and Diff regexes against the changes since the merge base with that base, including
uncommitted and untracked files. Reviewers run main's copy of this script too, so a change
can't edit what judges it (REVIEW.md):

    git show origin/main:tools/checklist.py | python3 -I -     # against origin/main
    uv run python tools/checklist.py --base main
    uv run python tools/checklist.py --file docs/reliability-checklist.md   # the local copy
    uv run python tools/checklist.py --list           # every item, no diff
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

CHECKLIST = "docs/reliability-checklist.md"
# Changed lines in these files don't trigger items: editing the list isn't a reason to run it all.
NO_DIFF_MATCH = {CHECKLIST, "REVIEW.md", "runtime/tests/test_reliability_checklist.py"}
FIELD = re.compile(r"^- \*\*(Paths|Diff|Check|From):\*\* (.*)$")
CODE = re.compile(r"^`(.+)`\s*$")


@dataclass
class Item:
    id: str
    title: str
    paths: re.Pattern[str] | None = None
    diff: re.Pattern[str] | None = None
    check: str = ""
    source: str = ""
    why: list[str] = field(default_factory=list)


def parse(text: str) -> list[Item]:
    """Items from the checklist's markdown; raises ValueError on a malformed item."""
    items: list[Item] = []
    last = None
    in_items = False
    for raw in text.splitlines():
        if raw.startswith("## "):
            in_items = raw.strip() == "## Items"
            continue
        if not in_items:
            continue
        if raw.startswith("### "):
            ident, sep, title = raw[4:].partition(":")
            if not sep or not re.fullmatch(r"[a-z0-9-]+", ident.strip()):
                raise ValueError(f"item heading must be '### <id>: <title>': {raw!r}")
            items.append(Item(ident.strip(), title.strip()))
            last = None
            continue
        if not items:
            continue
        item = items[-1]
        m = FIELD.match(raw)
        if m:
            name, value = m.group(1), m.group(2).strip()
            last = name
            if name in ("Paths", "Diff"):
                code = CODE.match(value)
                if not code:
                    raise ValueError(f"{item.id}: {name} must be one `regex`")
                try:
                    pattern = re.compile(code.group(1))
                except re.error as e:
                    raise ValueError(f"{item.id}: bad {name} regex: {e}") from e
                setattr(item, name.lower(), pattern)
            elif name == "Check":
                item.check = value
            else:
                item.source = value
        elif raw.startswith("  ") and raw.strip() and last in ("Check", "From"):
            if last == "Check":
                item.check += " " + raw.strip()
            else:
                item.source += " " + raw.strip()
        elif raw.strip():
            last = None
    seen = set()
    for item in items:
        if item.id in seen:
            raise ValueError(f"duplicate item id {item.id}")
        seen.add(item.id)
        if not item.paths and not item.diff:
            raise ValueError(f"{item.id}: needs a Paths or Diff trigger")
        if not item.check:
            raise ValueError(f"{item.id}: needs a Check")
    return items


_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def unquote(name: str) -> str | None:
    """A path git C-quoted (`"b/q\\"x.rs"`) as it is, or None if `name` isn't a quoted string.

    Git quotes a path holding `"`, `\\` or a control character (and non-ASCII bytes, unless
    core.quotePath is off), escaping those bytes as `\\"`, `\\\\`, `\\t` or three octal digits.
    """
    if len(name) < 2 or name[0] != '"' or name[-1] != '"':
        return None
    out = bytearray()
    i, body = 0, name[1:-1]
    while i < len(body):
        c = body[i]
        if c == '"':
            return None  # an unescaped quote inside: not one quoted string
        if c != "\\":
            out += c.encode("utf-8", "surrogateescape")
            i += 1
            continue
        nxt = body[i + 1 : i + 2]
        if nxt in _ESCAPES:
            out.append(_ESCAPES[nxt])
            i += 2
        elif re.fullmatch(r"[0-3][0-7]{2}", body[i + 1 : i + 4]):
            out.append(int(body[i + 1 : i + 4], 8))
            i += 4
        else:
            return None
    return out.decode("utf-8", "replace")


def header_path(rest: str, prefix: str) -> str | None:
    """The path in a `--- ` or `+++ ` header after the marker; None for /dev/null.

    A header git quoted is unquoted; one this can't parse is kept whole, so the file's
    lines are still matched rather than skipped.
    """
    rest = rest.rstrip("\t")  # git adds a tab after a path with spaces
    if rest == "/dev/null":
        return None
    unquoted = unquote(rest)
    if unquoted is not None:
        rest = unquoted
    return rest[len(prefix) :] if rest.startswith(prefix) else rest


def changed_lines(diff: str) -> list[tuple[str, str]]:
    """(path, line) for each added or removed line of a unified diff."""
    out = []
    path = ""
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
            path = ""
        elif not in_hunk and line.startswith(("--- ", "+++ ")):
            found = header_path(line[4:], "a/" if line[0] == "-" else "b/")
            if found is not None:
                path = found
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith(("+", "-")):
            # A hunk whose headers named no path is still matched, under "?".
            out.append((path or "?", line[1:]))
    return out


def match(items: list[Item], paths: list[str], lines: list[tuple[str, str]]) -> list[Item]:
    hits = []
    for item in items:
        why = []
        if item.paths:
            why += [f"path {p}" for p in paths if item.paths.search(p)]
        if item.diff:
            for path, line in lines:
                if path not in NO_DIFF_MATCH and item.diff.search(line):
                    why.append(f"line in {path}: {line.strip()[:100]}")
        if why:
            item.why = why
            hits.append(item)
    return hits


def git(*args: str) -> str:
    # quotePath off: non-ASCII paths come through as they are, not quoted and escaped.
    # (main() passes --text to diff so a .gitattributes `-diff` can't hide lines.)
    return subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        check=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",  # --text puts binary files' raw bytes in the diff
    ).stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", default="origin/main", help="branch to diff against (default: origin/main)")
    parser.add_argument("--file", help="read the checklist from this file instead of the base")
    parser.add_argument("--list", action="store_true", help="print every item and exit")
    args = parser.parse_args(argv)
    if args.file:
        args.file = os.path.abspath(args.file)  # before moving to the repo root

    try:
        os.chdir(git("rev-parse", "--show-toplevel").strip())
    except (OSError, subprocess.CalledProcessError):
        print("error: not in a git checkout", file=sys.stderr)
        return 2
    if args.file:
        try:
            with open(args.file, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            print(f"error: can't read {args.file}: {e.strerror}", file=sys.stderr)
            return 2
    else:
        try:
            text = git("show", f"{args.base}:{CHECKLIST}")
        except subprocess.CalledProcessError:
            print(f"error: no {CHECKLIST} on {args.base}; fetch it, or pass --file", file=sys.stderr)
            return 2
    try:
        items = parse(text)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.list:
        for item in items:
            print(f"{item.id}: {item.title}")
        return 0

    try:
        base = git("merge-base", args.base, "HEAD").strip()
        # Explicit prefixes: changed_lines() expects a/ and b/, which a user's
        # diff.mnemonicPrefix or diff.noprefix setting would otherwise change.
        diff = git(
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--no-renames",
            "--text",
            "--no-textconv",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "-U0",
            base,
        )
        untracked = [p for p in git("ls-files", "--others", "--exclude-standard", "-z").split("\0") if p]
        paths = {p for p in git("diff", "--name-only", "--no-renames", "-z", base).split("\0") if p}
    except subprocess.CalledProcessError as e:
        print(f"error: can't diff against {args.base}: {e.stderr.strip()}", file=sys.stderr)
        return 2
    paths |= set(untracked)
    lines = changed_lines(diff)
    # Untracked files are all added lines.
    for path in untracked:
        try:
            with open(path, encoding="utf-8") as f:
                lines += [(path, line) for line in f.read().splitlines()]
        except (OSError, UnicodeDecodeError):
            pass

    hits = match(items, sorted(paths), lines)
    source = args.file or f"{args.base}:{CHECKLIST}"
    print(f"{len(hits)} of {len(items)} checklist items match the diff against {args.base} ({source}).")
    for item in hits:
        print(f"\n[{item.id}] {item.title}")
        print(f"  Check: {item.check}")
        if item.source:
            print(f"  From:  {item.source}")
        shown = item.why[:3]
        more = len(item.why) - len(shown)
        print("  Matched: " + "; ".join(shown) + (f" (+{more} more)" if more else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
