# Reachy Hermes

Reachy Hermes runs the local Hermes Agent behind the Reachy Mini voice application.

## Pi Realtime tool harness

`reachy_hermes.pi_harness` is a small, provider-neutral bridge for a Realtime
client running on its **local host**. It is not a Hermes replacement and it
never starts a Reachy daemon, controller, media device, or robot action. It
starts the operator-managed local Pi CLI in documented JSONL RPC mode and
continues Pi's persisted session for that service working directory. A
Jetson/headless deployment is supported, but is not required.

The Python workspace intentionally does not install a similarly named PyPI
package. The supported external runtime contract is the local host's
`@mariozechner/pi-coding-agent` npm package at `0.73.1` (with Node 25.9.0 when
this integration was evaluated). The bridge verifies the package name and
version from its external `package.json` before launching it.

### Configuration and execution boundary

Pi's configuration and sessions are operator-owned and must never be committed.
Resolution is deliberately bounded:

1. `PI_CONFIG_PATH` — an existing absolute Pi **agent** directory. The bridge
   passes it through as Pi's documented `PI_CODING_AGENT_DIR`.
2. `~/.pi/agent` — the documented conventional location.
3. No Pi configuration override — only if neither location exists. Pi then
   receives its own vanilla configuration defaults.

There is no recursive home-directory scan and the bridge does **not** search
`PATH` for a `pi` executable. The normal external CLI is resolved only from
`<agent-dir>/npm/node_modules/@mariozechner/pi-coding-agent/dist/cli.js`.
Therefore a host using vanilla Pi defaults must set `PI_CLI_PATH` explicitly;
that path is launched without `PI_CODING_AGENT_DIR`. `PI_NODE_PATH` is available
when the local host's Node executable is not on `PATH`.

Register `realtime_functions()` with the Realtime client and route its structured
calls to `PiRealtimeHarness.dispatch()`:

- `get_pi_tool_inventory` obtains Pi's live extension/skill command inventory
  through RPC, lists only declared MCP server names, and makes that safe
  inventory available as `harness.last_inventory`. It never exposes MCP
  commands, arguments, URLs, configuration values, or secrets.
- `invoke_pi_session` accepts one `message` string (up to 4,000 characters),
  forwards it to Pi's continued local session, and returns compact assistant
  text plus a tool-call count. Extension UI dialogs are cancelled, never
  auto-approved.

Use `function.as_dict()` when registering the functions with the Realtime
session. When the client receives a function call, pass its `call_id`, `name`,
and JSON argument string to `adapt_realtime_function_call()`, send the returned
`conversation.item.create` event, then request the next Realtime response. The
adapter returns a compact safe error payload for local Pi failures so the voice
connection can continue.

Every call records only aggregate operation, success, elapsed milliseconds,
configuration source, command/tool-call counts, token totals, and cost. The
default local telemetry file is
`~/.local/state/reachy-hermes/pi-realtime.jsonl`; use `PI_TELEMETRY_PATH` to
select another operator-local path. Prompts, responses, session paths, config
contents, commands, credentials, and endpoints are intentionally not recorded.
The row also records whether a warm local Pi process was reused and, on failure,
only the error class.

The harness keeps one Pi RPC process warm while it is open, serializing calls
through that process. Call `await harness.aclose()` during application shutdown
or use `async with PiRealtimeHarness(...)`; cleanup first terminates the child
and then kills it if it does not exit promptly.

Do not register or invoke `invoke_pi_session` against hardware until the owner
has separately said to start hardware testing. This bridge contains no Reachy
control code, but Pi's externally declared tools are the source of truth and
may have their own authority.

### Evaluation contract

Before a non-actuating local-host evaluation, run only an inventory call and
compare the local telemetry row with the Realtime client's timing/token
telemetry. Confirm that the second inventory call reports
`session_reused: true`, then close the harness and verify no Pi child remains.
Keep the comparison in operator-local notes. A session prompt is a separate
action; use a harmless prompt first, verify its compact result and telemetry,
then stop. On a Jetson/headless host, the same evaluation contract applies.
Starting the Reachy controller, daemon, or robot hardware remains a separate
owner-authorized step.

## Accepted Hermes runtime

Reachy currently accepts Hermes Agent **v0.19.0 (2026.7.20)**. The tracked pin is in
`src/reachy_hermes/runtime_pin.py`: the required version is enforced while the build date is
recorded for the record only, the accepted
plugin set is `["reachy_probe"]`, and the vision model profile is `unrecorded` until the operator
runs the live compatibility gate on the robot host. Upstream and local SHAs are likewise
`None` until recorded; once either field is set, startup enforces it. The optional local-build
suffix reported by `hermes --version` is supported.

Hermes remains an operator-installed prerequisite. Reachy has no Hermes installer, updater,
package-manager integration, or `uv.lock` coupling. On the robot host, install the accepted
upstream release and verify the public identity:

```bash
ssh <robot-host>
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
git -C ~/.hermes/hermes-agent fetch --tags origin
git -C ~/.hermes/hermes-agent switch --detach v2026.7.20
cd ~/.hermes/hermes-agent
./setup-hermes.sh
~/.local/bin/hermes --version
```

The installer creates the prerequisite binary under `~/.local/bin`; Reachy never launches it
against the operator's ordinary `~/.hermes` state. Before `serve` starts, Reachy verifies the
binary and sets `HERMES_HOME` to `~/.cache/reachy-hermes/v<version>/home`. Its session working
directory is `~/.cache/reachy-hermes/v<version>/session`. Hermes does not honor `XDG_*` for these
locations, so `HERMES_HOME` is the isolation boundary. A mismatch rejects startup. For temporary
candidate testing only, `REACHY_HERMES_ALLOW_UNPINNED=1` permits a different parseable version and
logs a prominent warning; that version still receives its own isolated directory.

To roll back, deploy the repository revision containing the prior accepted pin and reinstall that
release:

```bash
ssh <robot-host>
git -C ~/.hermes/hermes-agent fetch --tags origin
git -C ~/.hermes/hermes-agent switch --detach <prior-release-tag>
cd ~/.hermes/hermes-agent
./setup-hermes.sh
~/.local/bin/hermes --version
```

When its version verifies, Reachy automatically resumes
`~/.cache/reachy-hermes/v<prior-version>/home` and
`~/.cache/reachy-hermes/v<prior-version>/session`; no state migration or copying is needed.

After a candidate passes both compatibility-gate commands below (including `--live` on
`<robot-host>`), copy the result's Hermes build identity and `model_profile` evidence into
`runtime_pin.py`, review the plugin list, and commit the pin change. Do not guess missing SHA or
model fields.

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
