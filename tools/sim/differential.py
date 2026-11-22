"""Differential sim run for reachy-mini bumps (#317).

    uv run python tools/sim/differential.py run BASE_LOCK CANDIDATE_LOCK [--work-dir DIR]
    python3 -I tools/sim/differential.py compare BASE.json CANDIDATE.json [--allow FILE]

``run`` installs each lock into a venv of its own, runs the sim checks
(``run.py --record``) against each, and compares what the daemon answered.
``compare`` does only the comparison, on two recordings made by
``run.py --record FILE``; it needs nothing beyond the standard library, so
it can judge recordings without running the code that made them.

A recording holds, for every daemon route the controller and ``smoke.py``
called during the run, the status codes it answered with and the shape of
its JSON bodies: each field's path and the JSON types seen there. Values are
left out, so timestamps, ids, ports, poses and versions never show up as
differences; ids used as path segments or object keys are folded into
``{id}``. Behaviour (states, motor modes, timing) is what ``smoke.py``
asserts, on both versions.

Any difference between the two recordings fails the comparison unless
``contract-changes.json`` (next to this file) lists it with a reason: a route
the controller relies on gaining or losing a field, a field changing type, a
status code appearing or disappearing, a route no longer being called.
Exit code: 0 when every difference is approved (and, for ``run``, both sim
runs passed).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ALLOW = HERE / "contract-changes.json"

# Path segments and object keys that are ids, not names: UUIDs, hex ids,
# numbers.
_ID = re.compile(
    r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{16,}|\d+)$"
)

Recording = dict[str, dict[str, Any]]


def route(method: str, path: str) -> str:
    """``METHOD /path?query`` with ids folded and query parameters sorted."""
    path, _, query = path.partition("?")
    path = "/".join("{id}" if _ID.match(part) else part for part in path.split("/"))
    params = "&".join(sorted(p for p in query.split("&") if p))
    return f"{method.upper()} {path}" + (f"?{params}" if params else "")


def _type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "array"
    return "object"


def shape(body: bytes, out: dict[str, set[str]] | None = None) -> dict[str, set[str]]:
    """Each field's path in a JSON body and the types seen there."""
    out = {} if out is None else out
    if not body.strip():
        out.setdefault("$", set()).add("empty")
        return out
    try:
        value = json.loads(body)
    except ValueError:
        out.setdefault("$", set()).add("text")
        return out

    def walk(path: str, value: Any) -> None:
        out.setdefault(path, set()).add(_type(value))
        if isinstance(value, dict):
            for key, item in value.items():
                walk(f"{path}.{'{id}' if _ID.match(str(key)) else key}", item)
        elif isinstance(value, list):
            for item in value:
                walk(f"{path}[]", item)

    walk("$", value)
    return out


class Recorder:
    """Status codes and body shapes per route, merged over a run."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[set[int], dict[str, set[str]]]] = {}

    def add(self, method: str, path: str, status: int, body: bytes) -> None:
        statuses, fields = self.routes.setdefault(route(method, path), (set(), {}))
        statuses.add(status)
        shape(body, fields)

    def dump(self) -> Recording:
        return {
            key: {"status": sorted(statuses), "fields": {p: sorted(t) for p, t in sorted(fields.items())}}
            for key, (statuses, fields) in sorted(self.routes.items())
        }


def differences(base: Recording, candidate: Recording) -> list[str]:
    """Every difference between two recordings, one line each."""
    out = []
    for key in sorted(base.keys() | candidate.keys()):
        if key not in candidate:
            out.append(f"{key}: no longer called")
            continue
        if key not in base:
            out.append(f"{key}: newly called")
            continue
        old, new = base[key], candidate[key]
        for status in sorted(set(old["status"]) - set(new["status"])):
            out.append(f"{key}: no longer answers {status}")
        for status in sorted(set(new["status"]) - set(old["status"])):
            out.append(f"{key}: now answers {status}")
        for path in sorted(old["fields"].keys() | new["fields"].keys()):
            was, now = old["fields"].get(path), new["fields"].get(path)
            if was == now:
                continue
            if now is None:
                out.append(f"{key}: {path} removed")
            elif was is None:
                out.append(f"{key}: {path} added ({'|'.join(now)})")
            else:
                out.append(f"{key}: {path} {'|'.join(was)} -> {'|'.join(now)}")
    return out


def load_allowed(path: Path) -> dict[str, str]:
    """Approved differences, each with the reason it was approved."""
    entries = json.loads(path.read_text())
    allowed = {}
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("difference") or not entry.get("reason"):
            raise ValueError(f"{path}: each entry needs a difference and a reason: {entry!r}")
        allowed[entry["difference"]] = entry["reason"]
    return allowed


def compare(base: Recording, candidate: Recording, allowed: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    """(unapproved, approved, approvals that matched nothing)."""
    found = differences(base, candidate)
    unapproved = [d for d in found if d not in allowed]
    approved = [d for d in found if d in allowed]
    unused = sorted(set(allowed) - set(found))
    return unapproved, approved, unused


def report(unapproved: list[str], approved: list[str], unused: list[str]) -> str:
    lines = []
    if unapproved:
        lines += ["Unapproved differences in the daemon's answers:", *(f"- {d}" for d in unapproved), ""]
    if approved:
        lines += ["Approved differences:", *(f"- {d}" for d in approved), ""]
    if unused:
        lines += ["Approvals that matched nothing (remove them once no bump needs them):"]
        lines += [*(f"- {d}" for d in unused), ""]
    if not (unapproved or approved):
        lines.append("No differences in the daemon's answers.")
    return "\n".join(lines)


def _compare_files(base: Path, candidate: Path, allow: Path) -> tuple[int, str]:
    unapproved, approved, unused = compare(
        json.loads(base.read_text()), json.loads(candidate.read_text()), load_allowed(allow)
    )
    return (1 if unapproved else 0), report(unapproved, approved, unused)


def _record(lock: Path, work: Path, name: str) -> int:
    """Install ``lock`` into a venv of its own and record a sim run on it."""
    project = work / f"{name}-project"
    project.mkdir(parents=True)
    shutil.copy(ROOT / "pyproject.toml", project / "pyproject.toml")
    shutil.copy(lock, project / "uv.lock")
    (project / "runtime").symlink_to(ROOT / "runtime")
    # Nothing of the venv this script runs in: under `uv run`, PYTHONPATH
    # holds its site-packages and would put its reachy-mini first.
    env = {k: v for k, v in os.environ.items() if k not in _OUTER_ENV}
    subprocess.run(["uv", "sync", "--frozen", "--quiet", "--project", str(project)], check=True, env=env)
    python = ["uv", "run", "--no-sync", "--project", str(project), "python"]
    probe = "import importlib.metadata as m; print(m.version('reachy-mini'))"
    installed = subprocess.run([*python, "-c", probe], env=env, capture_output=True, text=True, check=True)
    locked = lock_version(lock)
    if installed.stdout.strip() != locked:
        raise RuntimeError(f"{name}: {lock} locks reachy-mini {locked}, but the run imports {installed.stdout.strip()}")
    print(f"--- sim run on {name}: reachy-mini {locked} ({lock})", flush=True)
    command = [*python, str(HERE / "run.py"), "--work-dir", str(work / name), "--record", str(work / f"{name}.json")]
    return subprocess.run(command, env=env).returncode


_OUTER_ENV = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_RUN_RECURSION_DEPTH", "PYTHONPATH", "PYTHONHOME")


def lock_version(lock: Path) -> str:
    import tomllib

    packages = tomllib.loads(lock.read_text())["package"]
    return next(p["version"] for p in packages if p["name"] == "reachy-mini")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run", help="record a sim run on each lock, then compare")
    run.add_argument("base_lock", type=Path)
    run.add_argument("candidate_lock", type=Path)
    run.add_argument("--work-dir", type=Path, help="venvs, logs and recordings (default: a temp dir, kept)")
    run.add_argument("--allow", type=Path, default=ALLOW)
    cmp = sub.add_parser("compare", help="compare two recordings from run.py --record")
    cmp.add_argument("base", type=Path)
    cmp.add_argument("candidate", type=Path)
    cmp.add_argument("--allow", type=Path, default=ALLOW)
    args = parser.parse_args()

    if args.action == "compare":
        code, text = _compare_files(args.base, args.candidate, args.allow)
        print(text)
        return code

    work = (args.work_dir or Path(tempfile.mkdtemp(prefix="rb-diff-"))).resolve()
    work.mkdir(parents=True, exist_ok=True)
    runs = {
        name: _record(lock.resolve(), work, name)
        for name, lock in (("base", args.base_lock), ("candidate", args.candidate_lock))
    }
    failed = [name for name, code in runs.items() if code != 0]
    for name in failed:
        print(f"the sim checks failed on {name}; see {work / name}", file=sys.stderr)
    if not all((work / f"{name}.json").exists() for name in runs):
        return 1
    code, text = _compare_files(work / "base.json", work / "candidate.json", args.allow)
    print(text)
    print(f"recordings: {work / 'base.json'} {work / 'candidate.json'}")
    return 1 if failed else code


if __name__ == "__main__":
    sys.exit(main())
