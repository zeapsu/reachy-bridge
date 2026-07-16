# Live camera stream plan

Issue #3: https://github.com/zeapsu/reachy-console/issues/3

1. Add a focused `tools/reachy_mini_daemon_media.py` extension. Define a lazy shared MJPEG streamer with lock-protected latest JPEG state, bounded subscriber queues, a dedicated blocking reader/encoder thread, readiness gating, media-release recovery, and cancellation-safe shutdown. Wrap the daemon's original `create_app` and register `/api/camera/stream.mjpg`; call the installer from the Jetson wrapper before `daemon_main()`.
2. Add a true async HTTP stream context to `DaemonProxy`. Implement a controller camera lease manager and dedicated `/api/camera/stream` route that checks app ownership, forwards multipart chunks, maps transport/upstream failures to 503, and always closes upstream and leases. Register `close_all` as `AppSupervisor`'s camera release hook in `create_app`.
3. Update the static camera layer and viewport script. Keep the image source lazy and visibility/mode/daemon/availability gated; use load/error events and truthful placeholders; disconnect on every loss path and retry with a cache-busting query after recovery. Preserve all existing modes and avoid content filters.
4. Add focused daemon fake-reader tests, controller lease/proxy tests, and served-static behavior tests. Run only those tests plus the controller suite requested by the brief; run ASGI/fake-frame smoke checks and record exact output in `/tmp/reachy-console-issue-3-report.md`.
5. Self-review cancellation, ownership races, bounded state, and Jetson-only assumptions; commit all changes with messages referencing #3. Do not claim physical Jetson proof on macOS.
