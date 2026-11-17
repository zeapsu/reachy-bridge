"""tools/checklist.py: parsing docs/reliability-checklist.md and matching items to a diff."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("checklist", ROOT / "tools" / "checklist.py")
checklist = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["checklist"] = checklist  # dataclasses look the module up
_spec.loader.exec_module(checklist)

ITEMS = """# T

## Format

### not-an-item: in the format section
- **Paths:** `.*`
- **Check:** ignored

## Items

### ports: Ports
- **Diff:** `0\\.0\\.0\\.0`
- **Check:** binds stay on loopback,
  and the login covers the rest.
- **From:** #158

### units: Units
- **Paths:** `^deploy/systemd/`
- **Check:** telemetry off.
"""


def test_the_repo_checklist_parses():
    items = checklist.parse((ROOT / "docs" / "reliability-checklist.md").read_text())
    assert len(items) >= 10
    assert all(item.check and item.source for item in items)


def test_parse_reads_only_the_items_section_and_joins_continuations():
    items = checklist.parse(ITEMS)
    assert [i.id for i in items] == ["ports", "units"]
    assert items[0].check == "binds stay on loopback, and the login covers the rest."
    assert items[0].source == "#158"


@pytest.mark.parametrize(
    "text, message",
    [
        ("## Items\n### Bad Heading\n- **Paths:** `x`\n- **Check:** c\n", "heading"),
        ("## Items\n### a: A\n- **Check:** c\n", "trigger"),
        ("## Items\n### a: A\n- **Paths:** `x`\n", "Check"),
        ("## Items\n### a: A\n- **Paths:** `(`\n- **Check:** c\n", "regex"),
        ("## Items\n### a: A\n- **Paths:** x\n- **Check:** c\n", "one `regex`"),
        (
            "## Items\n### a: A\n- **Paths:** `x`\n- **Check:** c\n### a: B\n- **Paths:** `y`\n- **Check:** c\n",
            "duplicate",
        ),
    ],
)
def test_parse_rejects_malformed_items(text, message):
    with pytest.raises(ValueError, match=message):
        checklist.parse(text)


DIFF = """diff --git a/controller/src/main.rs b/controller/src/main.rs
--- a/controller/src/main.rs
+++ b/controller/src/main.rs
@@ -1 +1 @@
-    let addr = "127.0.0.1:7861";
+    let addr = "0.0.0.0:7861";
diff --git a/notes.txt b/notes.txt
--- a/notes.txt
+++ b/notes.txt
@@ -1,0 +1 @@
--- a removed line that looks like a header
diff --git a/docs/reliability-checklist.md b/docs/reliability-checklist.md
--- a/docs/reliability-checklist.md
+++ b/docs/reliability-checklist.md
@@ -1 +1 @@
+- **Diff:** `0\\.0\\.0\\.0`
"""


def test_changed_lines_tracks_paths_and_hunk_content():
    lines = checklist.changed_lines(DIFF)
    assert ("controller/src/main.rs", '    let addr = "0.0.0.0:7861";') in lines
    assert ("notes.txt", "-- a removed line that looks like a header") in lines
    assert all(path != "b/notes.txt" for path, _ in lines)


def test_match_by_line_and_path_but_not_from_the_checklist_itself():
    items = checklist.parse(ITEMS)
    lines = checklist.changed_lines(DIFF)
    assert [i.id for i in checklist.match(items, ["controller/src/main.rs"], lines)] == ["ports"]
    only_list = [(p, line) for p, line in lines if p == "docs/reliability-checklist.md"]
    assert checklist.match(checklist.parse(ITEMS), ["docs/reliability-checklist.md"], only_list) == []
    hits = checklist.match(checklist.parse(ITEMS), ["deploy/systemd/x.service"], [])
    assert [i.id for i in hits] == ["units"]


def test_main_reads_the_checklist_from_the_base_and_includes_untracked_files(tmp_path, monkeypatch, capsys):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "reliability-checklist.md").write_text(ITEMS)
    git("add", ".")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "change")
    # The branch empties the list; the base's copy still judges it.
    (tmp_path / "docs" / "reliability-checklist.md").write_text("## Items\n")
    (tmp_path / "deploy" / "systemd").mkdir(parents=True)
    (tmp_path / "deploy" / "systemd" / "new.service").write_text("[Service]\n")
    monkeypatch.chdir(tmp_path)
    assert checklist.main(["--base", "main"]) == 0
    out = capsys.readouterr().out
    assert "1 of 2 checklist items match" in out
    assert "[units]" in out


def test_main_fails_without_a_checklist_on_the_base(tmp_path, monkeypatch, capsys):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    assert checklist.main(["--base", "main"]) == 2
    assert "no docs/reliability-checklist.md" in capsys.readouterr().err


def test_changed_lines_strips_the_tab_git_adds_after_a_path_with_spaces():
    diff = "diff --git a/a b.txt b/a b.txt\n--- a/a b.txt\t\n+++ b/a b.txt\t\n@@ -0,0 +1 @@\n+x\n"
    assert checklist.changed_lines(diff) == [("a b.txt", "x")]


def test_main_reports_a_base_it_cannot_diff_against(tmp_path, monkeypatch, capsys):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    (tmp_path / "list.md").write_text(ITEMS)
    monkeypatch.chdir(tmp_path)
    assert checklist.main(["--base", "nope", "--file", "list.md"]) == 2
    assert "can't diff against nope" in capsys.readouterr().err
