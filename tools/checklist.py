"""Print the reliability checklist items whose triggers match a diff.

Reads docs/reliability-checklist.md from the base (origin/main by default), not from the
working tree, so a change can't edit the items it is judged by, and matches each item's
Paths and Diff regexes against the changes since the merge base with that base, including
uncommitted and untracked files. See REVIEW.md.

    uv run python tools/checklist.py                  # against origin/main
    uv run python tools/checklist.py --base main
    uv run python tools/checklist.py --file docs/reliability-checklist.md   # the local copy
    uv run python tools/checklist.py --list           # every item, no diff
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field

CHECKLIST = "docs/reliability-checklist.md"
# Changed lines in these files don't trigger items: editing the list isn't a reason to run it all.
NO_DIFF_MATCH = {CHECKLIST, "REVIEW.md"}
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


def changed_lines(diff: str) -> list[tuple[str, str]]:
    """(path, line) for each added or removed line of a unified diff."""
    out = []
    path = ""
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
            path = ""
        elif not in_hunk and line.startswith("--- a/"):
            path = line[6:]
        elif not in_hunk and line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith(("+", "-")) and path:
            out.append((path, line[1:]))
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
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", default="origin/main", help="branch to diff against (default: origin/main)")
    parser.add_argument("--file", help="read the checklist from this file instead of the base")
    parser.add_argument("--list", action="store_true", help="print every item and exit")
    args = parser.parse_args(argv)

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
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

    base = git("merge-base", args.base, "HEAD").strip()
    diff = git("diff", "--no-color", "--no-ext-diff", "-U0", base)
    untracked = git("ls-files", "--others", "--exclude-standard", "-z").split("\0")
    untracked = [p for p in untracked if p]
    paths = {p for p in git("diff", "--name-only", "--no-renames", "-z", base).split("\0") if p}
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
