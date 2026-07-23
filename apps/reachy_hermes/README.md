# Reachy Hermes

Reachy Hermes runs the local Hermes Agent behind the Reachy Mini voice application.

## Rerun the vision compatibility gate

From the repository root on a Mac development checkout:

```bash
uv sync --all-packages
uv run python -m reachy_hermes.compat_gate --json-out /tmp/hermes-vision-gate.json
uv run python -m reachy_hermes.compat_gate --json-out /tmp/hermes-vision-gate-live.json --live
```

To rerun the same committed gate on the robot host:

```bash
ssh <robot-host>
cd ~/reachy-console
uv sync --all-packages
uv run python -m reachy_hermes.compat_gate --json-out /tmp/hermes-vision-gate.json
uv run python -m reachy_hermes.compat_gate --json-out /tmp/hermes-vision-gate-live.json --live
```

The static run skips provider-dependent checks; skipped checks do not block `go`. With `--live`,
every requested live check must pass for `go` to be true. Both commands isolate Hermes state under a
temporary `HERMES_HOME` and report every compatibility failure in the JSON result.
