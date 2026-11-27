# Security

## Reporting a vulnerability

Report it privately through GitHub: the repository's **Security** tab, then **Report a
vulnerability**. Please don't open a public issue or pull request for it. Include what you found,
how to reproduce it, and which version or commit you ran.

## Supported versions

Fixes land on `main` and ship in the next release tag. Only the latest release gets them; there
are no backports.

## Scope

In scope, the code in this repository:

- the controller: login, pairing, sessions and admin mode, the app proxy, cross-site and framing
  checks, and anything it exposes beyond `127.0.0.1`;
- the runtime wrapper around the daemon, and the audio gateway;
- the installer, the uninstaller, the `reachy-bridge` command, the systemd units and udev rules;
- the CI workflows and the merge gates.

Out of scope:

- the Reachy Mini daemon and SDK (`reachy-mini`): report those to Pollen Robotics at
  [pollen-robotics/reachy_mini](https://github.com/pollen-robotics/reachy_mini);
- community apps installed from Hugging Face Spaces: report those to each app's author;
- Tailscale, the operating system and the board's firmware.

## Known limits

These are by design and documented in the README; reports that only restate them aren't
vulnerabilities, but ways around the protections described there are.

- Until an admin exists, anyone who reaches the dashboard can drive the robot
  ([Admin](README.md#admin)).
- LAN mode is plain HTTP, so anyone on that network can read its traffic, including the device
  cookie ([LAN access](README.md#lan-access)).
- Installed apps run as the bridge's own user and can read the controller's local key; the
  bridge moves an app's Python listeners onto loopback, but not native ones
  ([LAN access](README.md#lan-access)).
