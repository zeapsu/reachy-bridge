"""Command-line entry point for the Hermes vision compatibility gate."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Sequence

from . import assemble_result, make_check, run_gate


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", required=True, type=Path)
    parser.add_argument("--live", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = asyncio.run(run_gate(live=args.live))
    except BaseException as exc:
        result = assemble_result(
            {},
            [
                make_check(
                    "gate_execution",
                    "fail",
                    f"{type(exc).__name__}: {exc}",
                )
            ],
        )
    rendered = json.dumps(result, indent=2, sort_keys=False) + "\n"
    try:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(rendered, encoding="utf-8")
    except OSError as exc:
        result = assemble_result(
            result.get("hermes", {}),
            [
                *result.get("checks", []),
                make_check("json_output", "fail", str(exc)),
            ],
        )
        rendered = json.dumps(result, indent=2) + "\n"
    print(rendered, end="")
    return 0 if result.get("go") else 1


if __name__ == "__main__":
    raise SystemExit(main())
