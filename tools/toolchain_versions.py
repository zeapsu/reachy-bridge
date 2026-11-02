#!/usr/bin/env python3
"""Print mise.toml's tool versions as key=value lines for $GITHUB_OUTPUT.

Fails if a tool CI needs is missing or isn't a plain version string, so CI
can't quietly fall back to an unpinned version.
"""

import sys
import tomllib
from pathlib import Path

NEEDED = ("rust", "bun", "uv", "shellcheck")

tools = tomllib.loads((Path(__file__).parent.parent / "mise.toml").read_text())["tools"]
bad = [name for name in NEEDED if not isinstance(tools.get(name), str) or not tools[name]]
if bad:
    sys.exit(f'mise.toml: give {", ".join(bad)} as a plain version string, like rust = "1.96.0"')
for name in NEEDED:
    print(f"{name}={tools[name]}")
