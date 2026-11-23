# reachy-bridge

Run a **Reachy Mini Lite** from a **headless Linux edge device**, so the robot stays available
without a laptop plugged in. The bridge runs the upstream Reachy Mini daemon as an always-on
service, serves a controller UI you can reach from your other devices, and installs community
apps from Hugging Face Spaces.

> **Status:** in active development; not yet ready for general use. Tested on a Jetson Orin
> Nano running JetPack 7. Other Linux boards are experimental. Progress is tracked in
> [the release epic](https://github.com/zeapsu/reachy-bridge/issues/111).

## What's here today

```
runtime/                  Pollen's daemon plus the bridge's features and platform adapters
controller/               controller: Rust (axum) binary that embeds the dashboard (#144)
web/                      dashboard: React + TypeScript, built with Vite and Bun
deploy/                   installer, user units, and the reachy-bridge command
tools/                    sim integration tests, webrtcsink build for Jetson, udev rule
```

The controller talks to the daemon at `127.0.0.1:8000`. It switches the robot on and off,
plays expressions, shows telemetry, camera, audio and logs,
and starts or stops installed Hugging Face apps. A running app is opened in its own UI.

The daemon is Pollen's `reachy-mini`, tracked rather than pinned: `uv.lock` holds the tested
version and is bumped as Pollen releases (#155). Everything the bridge patches in the daemon
goes through `runtime/src/reachy_bridge_runtime/upstream.py`, and
`reachy-bridge-runtime --check` verifies those hooks against the installed version.

## Install on the device

```bash
deploy/install.sh        # runtime venv, controller, user units, the reachy-bridge command
reachy-bridge setup      # dashboard access, HF login, then the setup page (device check, robot test, admin)
reachy-bridge status
```

`reachy-bridge setup` first asks, on the terminal, how you'll reach the dashboard: over
Tailscale (recommended; it offers to run `reachy-bridge serve`) or on the local network (LAN
mode, which it turns on with a systemd drop-in that the uninstaller removes). It then asks for an
optional Hugging Face token, which you type on the terminal rather than on the setup page.

Next it opens a temporary page on the local network (port 7870) and prints a one-time code.
Enter the code on the page from any computer or phone on the network. The page shows the device
check, then offers the robot test: after you confirm on the terminal, the robot powers on without
the wake-up move (the motors stay limp; nothing moves), its motors are read, and it powers off.
The test runs only while the robot is off. Last, the page asks for the pairing code your
dashboard shows; the terminal names that dashboard and asks you to type the same code there, and
nothing is approved until the code typed on the robot matches.

The setup code is single use and works for 15 minutes; five wrong tries lock the page until you
press Enter in the terminal for a new code. If someone on the network keeps locking it,
`reachy-bridge pair --admin <code>` on the robot makes your dashboard the admin without the page.
The page is plain HTTP and listens on every network the device is on while it is open, even when
the dashboard is tailnet-only, which is why it is short-lived. It closes when you finish, when you
quit the command, or half an hour after the code was used.

`reachy-bridge serve` adds two HTTPS ports with `tailscale serve`: :10000 for the dashboard and
:8043 for the running app's own UI. App UIs listen on plain http (`:8042`, `:7860`, ...), where
browsers block the microphone and camera; the controller proxies whichever app is running on
`127.0.0.1:7863`, and a dashboard opened over HTTPS sends you to `https://<host>:8043/`. Over
plain http the dashboard opens `http://<host>:7863/`. The bridge keeps app web servers and WebRTC
signalling (`:8443`) on loopback, so the proxy is the way to an app from another machine (see the
limits under LAN access). Run
`reachy-bridge serve` again after an update that adds a mapping.

If the dashboard's tailnet name won't load on an iPhone but works on other devices, turn off iCloud
Private Relay (and "Limit IP Address Tracking" for the network). It sends Safari's DNS lookups past
Tailscale, so public DNS answers for the name, and that answer never points at the device.

The robot starts off; switch it on from the dashboard. `install.sh` is safe to re-run after a
pull, and never restarts the daemon while the robot is on.

To remove the bridge, run `reachy-bridge uninstall` (or `deploy/uninstall.sh`). It puts the robot
to sleep, removes the services, the install, installed apps, settings and paired devices, and the
tailnet ports that still point at the bridge, then lists what it left (Tailscale, apt packages,
the Hugging Face cache). `--dry-run` shows the plan; `--keep-data` keeps apps, settings and paired
devices for a reinstall.

## LAN access

By default the dashboard listens on loopback only and is reached over Tailscale. To open it to
the home network as well, run `reachy-bridge setup` and choose `l`: it sets `CONTROLLER_LAN=1`
for the controller in a systemd drop-in (removed by `reachy-bridge uninstall`) and restarts it.

The dashboard then listens on every interface on port 7861, and the app UI proxy on port 7863;
the daemon, the app UIs and WebRTC signalling stay on loopback.
A browser on the LAN gets a login screen with a 6-digit code (valid for 5 minutes). Approve it
on the device with `reachy-bridge pair <code> [name]`, or from an already-paired dashboard
(devices panel). The browser then keeps a device cookie for a year. `reachy-bridge devices`
lists paired browsers and `reachy-bridge devices revoke <n>` logs one out at once (`<n>` is its number in that list; the id works too).
Visitors through `tailscale serve` and local tools are trusted without pairing; set
`CONTROLLER_TRUST_TAILNET=0` to make them pair too (an admin, below, does that as well). The same login covers the app UIs, on :7863
from the LAN and on :8043 through `tailscale serve`.

Apps are code you install from Hugging Face, and they run as the bridge's own user. The bridge
moves every server an app opens through Python's `socket` module onto loopback, but not listeners
opened by native code (a C extension, a Node or ffmpeg child process), and not a Gradio app's
public `share` link. An app can also read the controller's local key. Install apps you trust.

A paired browser can approve and revoke other browsers, so approve only codes you expect
(`reachy-bridge pair` prints the address that asked). A browser unused for 30 days is logged out.

## Admin

Until an admin exists, anyone who reaches the dashboard can drive the robot. To lock it to the
browsers you approve, LAN mode or not, make your own browser the admin: open the devices panel
(System), choose "Make this browser the admin", and run the command it shows on the robot,
`reachy-bridge pair --admin --person=<you> <code> [name]`. Only the CLI on the robot can approve the
first admin. After that an admin browser can approve another browser as an admin, with its code, or
make an approved one an admin (System › Access), so a stolen admin browser can add admins of its own:
revoke a lost device from another admin browser or from the robot.

Browsers paired before that are logged out, since anyone could approve them. From then on
every browser logs in, Tailscale visitors included, and open streams and drive sessions from
browsers without a login end at once. Only the admin approves new browsers (with a device name
and the person using it) and revokes them, installs, updates and removes apps, sets the Hugging
Face token, and switches the kinematics engine. Other people's browsers
can log themselves out. The dashboard won't revoke the last admin device or make it a member; to recover a lost one,
pair a new admin from the robot and revoke the old one with `reachy-bridge devices revoke <n>`.
Admin mode stays on even with no admin device left.

In admin mode one browser at a time drives the robot. Any approved browser takes control with its
first robot action while nobody else has it, and keeps it until it lets go (closing the dashboard
does), does nothing with the robot for 10 minutes, or goes quiet for a minute; the admin can always
take over. A browser that went quiet gets control back when it returns, unless someone took it. Approving a browser is what lets someone drive, and
revoking it takes that away (and frees the robot at once if that browser was driving).

An installed app runs as the bridge's user
and can read the local key, so it could approve an admin too; install apps you trust.

LAN traffic is plain HTTP, so anyone on the network can read it, including the device cookie,
unless you put TLS in front of the controller. The app proxy strips the cookie before a request
reaches an app. Use LAN access on a home
network you trust, and Tailscale (`reachy-bridge serve`, HTTPS) for anything beyond it.

## Listen and talk

The Robot tab's Voice pane plays the robot's microphone on your device and your
device's microphone through the robot's speaker. It can also play what the robot says (its own
sounds and an app's voice) on your device instead of its speaker, and send your voice to the
robot's apps, mixed into what they hear as its microphone, so you can hold a conversation with an
app from anywhere. Everything that reads the robot's microphone hears you, including a remote
session connected through the daemon, and nothing in the room signals it; the dashboards' badge
does. (Talking through the speaker doesn't reach apps: the robot's audio board
cancels its own speaker output from its microphone.) Each is switched while connected. One
browser at a time, with the robot on; in admin mode, only the browser in control, and the session
ends when control or the login does, or when the robot is turned off. Every dashboard shows a mic
badge while a session is open.

The controller starts `reachy-bridge-audio.service` on demand. `reachy-bridge-mic.service` runs with the daemon and brings the real microphone onto the loopback card, where a browser's voice mixes with it; with a loopback card, apps read the microphone there. Audio is WebRTC over UDP straight
between the browser and the device, on tailnet addresses only (no STUN or TURN), using UDP ports
47100-47199; a Tailscale ACL that limits ports must allow them. Use a tailnet address: audio
can't connect from a home-network address. Listening also works on the device's plain-http
tailnet IP (LAN mode), and so does hearing what the robot says, but talking (to the robot or its
apps) needs the HTTPS (Tailscale) name, since browsers only share a microphone with a secure page. Sim mode has no audio. The device needs GStreamer's libnice plugin
(`sudo apt install gstreamer1.0-nice`); `reachy-bridge check` reports it. Apps that use sounddevice need PortAudio (`sudo apt install libportaudio2`); the check warns when it is missing.

Hearing what the robot says and talking to its apps need an ALSA loopback card with the id
`Loopback`, which the runtime then routes the robot's speaker output and microphone through (it
rewrites upstream's `~/.asoundrc`, keeping its device names, and takes the change out again when
the card is gone). It also needs `alsaloop` (`sudo apt install alsa-utils`); `reachy-bridge check` warns when it is missing. Load the card now and at boot:

```bash
sudo modprobe snd-aloop index=10 id=Loopback pcm_substreams=2
echo snd-aloop | sudo tee /etc/modules-load.d/reachy-bridge-loopback.conf
echo "options snd-aloop index=10 id=Loopback pcm_substreams=2" | sudo tee /etc/modprobe.d/reachy-bridge-loopback.conf
```

then reinstall with the robot off (`deploy/install.sh`), so the daemon picks up the new
`~/.asoundrc`. `reachy-bridge check` reports the card.

## Development

Setup, running against the simulator, tests, the daemon seam and how releases work are in
[`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

Apache-2.0; see [`LICENSE`](LICENSE). [`NOTICE`](NOTICE) credits the Pollen Robotics material
the bridge builds on.
