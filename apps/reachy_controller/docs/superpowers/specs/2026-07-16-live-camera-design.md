# Live camera stream design

Issue #3: https://github.com/zeapsu/reachy-console/issues/3

## Contract

The Jetson wrapper patches the Reachy Mini daemon's `create_app` before the daemon entry point runs. The patch adds `GET /api/camera/stream.mjpg`, served as `multipart/x-mixed-replace; boundary=frame`. The route owns one lazy `GStreamerCamera` IPC consumer for every HTTP subscriber. A dedicated reader thread performs blocking `open()`/`read()` calls and JPEG encoding; the event loop only coordinates bounded latest-frame delivery.

The reader starts on the first subscriber and stops/closes after the last disconnect. Shared state (latest JPEG, sequence, subscriber count, reader state) is lock guarded. Media readiness requires a daemon media server, media not released, and an available local camera IPC socket. Unavailable media, missing optional dependencies, and reader failures are surfaced honestly as HTTP 503 before a stream starts; a released/reacquired daemon can recover without a second hardware capture.

The controller keeps `/api/camera/stream` as a dedicated streaming proxy. It preserves the daemon multipart content type, forwards chunks without buffering, closes the upstream response on disconnect/cancellation, and reports upstream/offline failures as 503. A controller-owned dashboard lease manager tracks active proxy streams. `AppSupervisor` invokes its release hook before spawning `reachy_chat` or `reachy_lookat`; a camera app returns an actionable 409 while loading/running. Stopping an app only makes the camera available again; the frontend decides whether to reconnect.

The browser does not set an image `src` until camera mode is active, the document is visible, and availability reports available. It removes `src` on mode exit, tab hiding, daemon offline, ownership loss, load failure, or upstream error. Image load/error events control the placeholder; availability recovery retries with a cache-busting query. Camera copy distinguishes live/loading/in-use/offline/error states. The existing backdrop refraction remains behind content and no `filter` is applied to `.vp-stage` or the camera image. Reduced-motion/transparency and other viewport modes remain unchanged.

## Safety and verification

Cancellation is handled in both streaming layers: browser source removal closes the HTTP request, controller generator finalizers close the daemon response and lease, and daemon subscriber cleanup stops the reader thread. Per-subscriber queues are bounded to one latest frame. The Mac test environment uses fake frames/readers and ASGI transport; it cannot prove NVIDIA IPC or physical camera behavior. Jetson functional verification remains a deployment checklist item.
