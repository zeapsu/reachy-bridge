# Reachy Hermes

Reachy Hermes runs the local Hermes Agent behind the Reachy Mini voice application.

## Pi Realtime tool harness

`reachy_hermes.pi_harness` is a small, non-integrated boundary for a future
Pi-backed Realtime experiment. It is not a Hermes replacement and it does not
start an agent, provider, gateway, daemon, or robot action. Its only public
factory requires an explicit `PiRealtimeHarnessConfig`; it does not read
environment variables, user files, or global Pi configuration.

The harness exposes exactly one typed tool,
`get_realtime_tool_capabilities`. It accepts no arguments and returns only a
deterministic description of its fixed allowlist. Unknown, malformed, or
argument-bearing calls fail closed. It has no shell, process, network,
filesystem, media, daemon, or controller access.

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
