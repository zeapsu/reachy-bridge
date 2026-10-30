//! LAN login (#141), in-process against a minimal fake daemon.
//!
//! Peers are simulated by inserting `ConnectInfo<SocketAddr>` into each
//! request's extensions, which is where `into_make_service_with_connect_info`
//! puts the real socket address in production.

use std::net::SocketAddr;
use std::sync::atomic::{AtomicU64, AtomicU8, Ordering};
use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::extract::ConnectInfo;
use axum::http::{HeaderMap, Request, StatusCode};
use axum::routing::get;
use axum::{Json, Router};
use futures_util::StreamExt;
use reachy_controller::audio;
use reachy_controller::auth::AuthConfig;
use reachy_controller::daemon::Daemon;
use reachy_controller::logs::LogBroker;
use reachy_controller::state::{AppState, Shared};
use serde_json::{json, Value};
use tower::ServiceExt;

const LOOPBACK: &str = "127.0.0.1:50000";
const LAN: &str = "192.168.1.50:50000";
const LAN2: &str = "192.168.1.51:50000";

/// A daemon whose robot is off until `on` is set.
// The fake robot's power: ROBOT_OFF, ROBOT_ON, ROBOT_CRASHED (its backend
// died), ROBOT_STARTING, or ROBOT_SILENT (the daemon doesn't answer).
const ROBOT_OFF: u8 = 0;
const ROBOT_ON: u8 = 1;
const ROBOT_CRASHED: u8 = 2;
const ROBOT_STARTING: u8 = 3;
const ROBOT_SILENT: u8 = 4;

async fn fake_daemon(power: Arc<AtomicU8>) -> String {
    let app = Router::new().route(
        "/api/daemon/status",
        get(move || async move {
            let (state, backend) = match power.load(Ordering::SeqCst) {
                ROBOT_ON => ("running", json!({"motor_control_mode": "enabled"})),
                ROBOT_CRASHED => ("error", Value::Null),
                ROBOT_STARTING => ("starting", Value::Null),
                ROBOT_SILENT => return Err(StatusCode::SERVICE_UNAVAILABLE),
                _ => ("stopped", Value::Null),
            };
            Ok(Json(json!({"state": state, "backend_status": backend, "error": null})))
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    url
}

struct TempDir(std::path::PathBuf);

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

struct Harness {
    state: Shared,
    dir: TempDir,
    /// The fake robot's power.
    robot: Arc<AtomicU8>,
}

struct Reply {
    status: StatusCode,
    headers: HeaderMap,
    body: Value,
}

impl Reply {
    fn set_cookies(&self) -> Vec<String> {
        self.headers.get_all("set-cookie").iter().map(|v| v.to_str().unwrap().to_string()).collect()
    }

    /// `name=value` of the Set-Cookie for `name` (for a Cookie header).
    fn cookie(&self, name: &str) -> String {
        let prefix = format!("{name}=");
        let line = self.set_cookies().into_iter().find(|c| c.starts_with(&prefix)).expect("cookie set");
        line.split(';').next().unwrap().to_string()
    }
}

async fn harness(config: AuthConfig) -> Harness {
    let robot = Arc::new(AtomicU8::new(ROBOT_OFF));
    let url = fake_daemon(robot.clone()).await;
    // A counter, not the clock: macOS clocks tick in microseconds, so tests
    // starting together got the same directory and deleted each other's.
    static NEXT: AtomicU64 = AtomicU64::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("rc-auth-{}-{n}", std::process::id()));
    let state = AppState::with_auth(
        Daemon::new(&url, Duration::from_secs(2)),
        LogBroker::new(),
        dir.clone(),
        Duration::from_millis(10),
        config,
    );
    Harness { state, dir: TempDir(dir), robot }
}

fn lan() -> AuthConfig {
    AuthConfig { lan: true, ..AuthConfig::default() }
}

fn request(method: &str, uri: &str, peer: &str, headers: &[(&str, &str)], body: Option<Value>) -> Request<Body> {
    let peer_addr = peer.parse::<SocketAddr>().unwrap();
    let mut builder = Request::builder().method(method).uri(uri);
    if !headers.iter().any(|(k, _)| k.eq_ignore_ascii_case("host")) {
        // Local tools use 127.0.0.1; LAN browsers the robot's LAN address.
        builder = builder.header("host", if peer_addr.ip().to_canonical().is_loopback() { "127.0.0.1:7861" } else { "10.0.0.2:7861" });
    }
    for (k, v) in headers {
        builder = builder.header(*k, *v);
    }
    if body.is_some() {
        builder = builder.header("content-type", "application/json");
    }
    let mut request = builder.body(body.map_or_else(Body::empty, |b| Body::from(b.to_string()))).unwrap();
    request.extensions_mut().insert(ConnectInfo(peer_addr));
    request
}

impl Harness {
    async fn send(&self, request: Request<Body>) -> Reply {
        let response = reachy_controller::app(self.state.clone()).oneshot(request).await.unwrap();
        let status = response.status();
        let headers = response.headers().clone();
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let body = serde_json::from_slice(&bytes).unwrap_or_else(|_| Value::String(String::from_utf8_lossy(&bytes).into()));
        Reply { status, headers, body }
    }

    async fn call(&self, method: &str, uri: &str, peer: &str, headers: &[(&str, &str)], body: Option<Value>) -> Reply {
        self.send(request(method, uri, peer, headers, body)).await
    }

    /// POST /api/auth/pair from `peer`: (id, code, pair cookie).
    async fn pair(&self, peer: &str) -> (String, String, String) {
        let r = self.call("POST", "/api/auth/pair", peer, &[], None).await;
        assert_eq!(r.status, StatusCode::OK, "{:?}", r.body);
        assert_eq!(r.body["expires_in"], self.state.auth.config.pair_ttl.as_secs());
        (r.body["id"].as_str().unwrap().into(), r.body["code"].as_str().unwrap().into(), r.cookie("rb_pair"))
    }

    async fn approve(&self, code: &str, peer: &str, headers: &[(&str, &str)]) -> Reply {
        self.call("POST", "/api/auth/approve", peer, headers, Some(json!({"code": code, "name": "phone"}))).await
    }

    async fn poll(&self, id: &str, peer: &str, cookie: &str) -> Reply {
        self.call("GET", &format!("/api/auth/pair/{id}"), peer, &[("cookie", cookie)], None).await
    }

    /// Pair a LAN browser end to end; returns its `rb_device=...` cookie.
    async fn paired(&self, peer: &str) -> String {
        let (id, code, pair_cookie) = self.pair(peer).await;
        assert_eq!(self.approve(&code, LOOPBACK, &[]).await.status, StatusCode::OK);
        let r = self.poll(&id, peer, &pair_cookie).await;
        assert_eq!(r.body["status"], "approved");
        r.cookie("rb_device")
    }

    fn key(&self) -> String {
        std::fs::read_to_string(self.dir.0.join("controller.key")).unwrap().trim().to_string()
    }

    /// Pair a browser at `peer` with `body` approved by `approver`; returns
    /// its `rb_device=...` cookie.
    async fn paired_as(&self, peer: &str, approver: &[(&str, &str)], body: Value) -> String {
        let (id, code, pair_cookie) = self.pair(peer).await;
        let mut body = body;
        body["code"] = json!(code);
        let r = self.call("POST", "/api/auth/approve", LOOPBACK, approver, Some(body)).await;
        assert_eq!(r.status, StatusCode::OK, "{:?}", r.body);
        self.poll(&id, peer, &pair_cookie).await.cookie("rb_device")
    }

    /// Pair the admin's browser the way `reachy-bridge pair --admin` does.
    async fn admin(&self, peer: &str) -> String {
        let key = self.key();
        self.paired_as(peer, &[("x-reachy-bridge-key", &key)], json!({"name": "laptop", "person": "Andry", "role": "admin"})).await
    }

    fn devices_file(&self) -> String {
        std::fs::read_to_string(self.dir.0.join("devices.json")).unwrap_or_default()
    }
}

#[tokio::test]
async fn lan_off_checks_nothing() {
    let h = harness(AuthConfig::default()).await;
    for peer in [LOOPBACK, LAN] {
        let r = h.call("GET", "/api/daemon/status", peer, &[], None).await;
        assert_eq!(r.status, StatusCode::OK);
        assert_eq!(h.call("GET", "/api/logs/sources", peer, &[], None).await.status, StatusCode::OK);
    }
    // Requests without any ConnectInfo (as tests/api.rs sends them) still work.
    let r = h.send(Request::get("/api/logs/sources").body(Body::empty()).unwrap()).await;
    assert_eq!(r.status, StatusCode::OK);
    let r = h.call("GET", "/api/auth/status", LAN, &[], None).await;
    assert_eq!(
        r.body,
        json!({"lan": false, "login_required": false, "admin_mode": false, "authenticated": true, "device": null, "person": null, "role": null})
    );
    // Pairing works without LAN mode, and the key is there for `pair --admin`.
    h.pair(LOOPBACK).await;
    assert!(h.dir.0.join("controller.key").exists());
}

#[tokio::test]
async fn lan_off_refuses_a_rebinding_host_on_loopback() {
    let h = harness(AuthConfig::default()).await;
    for host in ["127.0.0.1:7861", "localhost:7861", "robot.tail1234.ts.net"] {
        let r = h.call("POST", "/api/apps/stop/x", LOOPBACK, &[("host", host)], None).await;
        assert_ne!(r.status, StatusCode::FORBIDDEN, "{host}");
    }
    for host in ["evil.example:7861", "127.0.0.1.evil.example"] {
        for (method, uri) in [("GET", "/api/logs/sources"), ("PUT", "/api/hf/token"), ("POST", "/api/apps/install")] {
            let r = h.call(method, uri, LOOPBACK, &[("host", host), ("sec-fetch-site", "same-origin")], None).await;
            assert_eq!(r.status, StatusCode::FORBIDDEN, "{method} {uri} via {host}");
        }
    }
    // The page itself still loads (it then shows the API's refusals).
    assert_ne!(h.call("GET", "/", LOOPBACK, &[("host", "evil.example")], None).await.status, StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn lan_on_trusts_loopback_and_locks_out_lan_peers() {
    let h = harness(lan()).await;
    assert_eq!(h.call("GET", "/api/daemon/status", LOOPBACK, &[], None).await.status, StatusCode::OK);
    // IPv4-mapped IPv6 loopback is loopback.
    assert_eq!(h.call("GET", "/api/logs/sources", "[::ffff:127.0.0.1]:1", &[], None).await.status, StatusCode::OK);
    assert_eq!(h.call("GET", "/api/logs/sources", "[::1]:1", &[], None).await.status, StatusCode::OK);

    for uri in ["/api/daemon/status", "/api/logs/sources", "/telemetry/state-stream", "/api/auth/devices"] {
        let r = h.call("GET", uri, LAN, &[], None).await;
        assert_eq!(r.status, StatusCode::UNAUTHORIZED, "{uri}");
        assert_eq!(r.body, json!({"detail": "login required"}));
    }
    assert_eq!(h.call("POST", "/api/daemon/start", LAN, &[], None).await.status, StatusCode::UNAUTHORIZED);
    // X-Forwarded-For can't make a LAN peer look local.
    let spoof = [("x-forwarded-for", "127.0.0.1"), ("x-real-ip", "127.0.0.1"), ("forwarded", "for=127.0.0.1")];
    assert_eq!(h.call("GET", "/api/logs/sources", LAN, &spoof, None).await.status, StatusCode::UNAUTHORIZED);
    // A mapped LAN address is not loopback.
    assert_eq!(h.call("GET", "/api/logs/sources", "[::ffff:192.168.1.50]:1", &[], None).await.status, StatusCode::UNAUTHORIZED);
    // No ConnectInfo at all fails closed.
    let r = h.send(Request::get("/api/logs/sources").body(Body::empty()).unwrap()).await;
    assert_eq!(r.status, StatusCode::UNAUTHORIZED);

    // Pages still load (the dashboard shows its login screen); auth status is open.
    let r = h.call("GET", "/", LAN, &[], None).await;
    assert_ne!(r.status, StatusCode::UNAUTHORIZED);
    let r = h.call("GET", "/api/auth/status", LAN, &[], None).await;
    assert_eq!((r.body["login_required"].clone(), r.body["authenticated"].clone()), (json!(true), json!(false)));
    let r = h.call("GET", "/api/auth/status", LOOPBACK, &[], None).await;
    assert_eq!(r.body["authenticated"], true);
}

#[tokio::test]
async fn pairing_flow_issues_a_working_cookie() {
    let h = harness(lan()).await;
    let (id, code, pair_cookie) = h.pair(LAN).await;
    assert_eq!(code.len(), 6);
    assert_eq!(h.poll(&id, LAN, &pair_cookie).await.body, json!({"status": "pending"}));

    let r = h.approve(&code, LOOPBACK, &[]).await;
    assert_eq!(r.status, StatusCode::OK, "{:?}", r.body);
    let r = h.poll(&id, LAN, &pair_cookie).await;
    assert_eq!(r.body["status"], "approved");
    let line = r.set_cookies().into_iter().find(|c| c.starts_with("rb_device=")).unwrap();
    assert!(line.contains("HttpOnly") && line.contains("SameSite=Strict") && line.contains("Path=/;"), "{line}");
    assert!(line.contains("Max-Age=31536000") && !line.contains("Secure"), "{line}");
    let token = r.cookie("rb_device")["rb_device=".len()..].to_string();
    assert_eq!(token.len(), 43, "32 bytes, base64url");
    let device = r.cookie("rb_device");

    // The cookie works from any LAN address; the request is gone after delivery.
    assert_eq!(h.call("GET", "/api/logs/sources", LAN2, &[("cookie", &device)], None).await.status, StatusCode::OK);
    let r = h.call("GET", "/api/auth/status", LAN, &[("cookie", &device)], None).await;
    assert_eq!(r.body["authenticated"], true);
    assert_eq!(h.poll(&id, LAN, &pair_cookie).await.status, StatusCode::NOT_FOUND);
    let r = h.call("GET", "/api/auth/devices", LAN, &[("cookie", &device)], None).await;
    let devices = r.body["devices"].as_array().unwrap();
    assert_eq!(devices.len(), 1);
    assert_eq!(devices[0]["name"], "phone");
    assert_eq!(devices[0]["peer"], "192.168.1.50");
    assert_eq!(devices[0]["current"], true);

    // Stored hashed, file mode 0600.
    let file = h.devices_file();
    assert!(!file.contains(&token), "raw token in devices.json");
    assert!(file.contains("token_sha256"));
    use std::os::unix::fs::PermissionsExt;
    let mode = std::fs::metadata(h.dir.0.join("devices.json")).unwrap().permissions().mode();
    assert_eq!(mode & 0o777, 0o600);
    let names: Vec<_> = std::fs::read_dir(&h.dir.0).unwrap().map(|e| e.unwrap().file_name()).collect();
    assert!(names.iter().all(|n| !n.to_string_lossy().contains(".tmp")), "temp file left: {names:?}");
}

#[tokio::test]
async fn devices_survive_a_restart() {
    let h = harness(lan()).await;
    let device = h.paired(LAN).await;
    let state = AppState::with_auth(Daemon::new("http://127.0.0.1:9", Duration::from_millis(200)), LogBroker::new(), h.dir.0.clone(), Duration::from_millis(10), lan());
    let restarted = Harness { state, dir: TempDir(std::env::temp_dir().join("rc-auth-unused")), robot: Default::default() };
    assert_eq!(restarted.call("GET", "/api/logs/sources", LAN, &[("cookie", &device)], None).await.status, StatusCode::OK);
}

#[tokio::test]
async fn a_paired_device_can_approve_another() {
    let h = harness(lan()).await;
    let device = h.paired(LAN).await;
    let (id, code, pair_cookie) = h.pair(LAN2).await;
    // A LAN peer without a cookie can't approve.
    assert_eq!(h.approve(&code, LAN2, &[]).await.status, StatusCode::UNAUTHORIZED);
    assert_eq!(h.approve(&code, LAN, &[("cookie", &device)]).await.status, StatusCode::OK);
    assert_eq!(h.poll(&id, LAN2, &pair_cookie).await.body["status"], "approved");
}

#[tokio::test]
async fn five_wrong_codes_lock_a_request() {
    let h = harness(lan()).await;
    let (id, code, pair_cookie) = h.pair(LAN).await;
    let wrong = if code == "000000" { "000001" } else { "000000" };
    for _ in 0..5 {
        assert_eq!(h.approve(wrong, LOOPBACK, &[]).await.status, StatusCode::BAD_REQUEST);
    }
    assert_eq!(h.poll(&id, LAN, &pair_cookie).await.body["status"], "locked");
    // The right code no longer works.
    assert_eq!(h.approve(&code, LOOPBACK, &[]).await.status, StatusCode::BAD_REQUEST);
    assert_eq!(h.poll(&id, LAN, &pair_cookie).await.body["status"], "locked");
    // Malformed codes are refused without a lookup.
    assert_eq!(h.approve("12a456", LOOPBACK, &[]).await.status, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn codes_expire() {
    let h = harness(AuthConfig { pair_ttl: Duration::from_millis(200), ..lan() }).await;
    let (id, code, pair_cookie) = h.pair(LAN).await;
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(h.poll(&id, LAN, &pair_cookie).await.body["status"], "expired");
    assert_eq!(h.approve(&code, LOOPBACK, &[]).await.status, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn polling_needs_the_browser_secret() {
    let h = harness(lan()).await;
    let (id, code, pair_cookie) = h.pair(LAN).await;
    assert_eq!(h.approve(&code, LOOPBACK, &[]).await.status, StatusCode::OK);
    // The id alone, or with another browser's secret, gets nothing.
    let (_, _, other_cookie) = h.pair(LAN2).await;
    for cookie in ["", "rb_pair=guess", other_cookie.as_str()] {
        let r = h.poll(&id, LAN2, cookie).await;
        assert_eq!(r.status, StatusCode::NOT_FOUND);
        assert!(r.set_cookies().iter().all(|c| !c.starts_with("rb_device=")));
    }
    // The pair cookie is scoped to its own poll URL.
    let r = h.call("POST", "/api/auth/pair", LAN, &[], None).await;
    let line = r.set_cookies().into_iter().find(|c| c.starts_with("rb_pair=")).unwrap();
    assert!(line.contains(&format!("Path=/api/auth/pair/{}", r.body["id"].as_str().unwrap())) && line.contains("HttpOnly"));
    // The creating browser still collects its token.
    assert_eq!(h.poll(&id, LAN, &pair_cookie).await.body["status"], "approved");
}

#[tokio::test]
async fn revoking_takes_effect_immediately() {
    let h = harness(lan()).await;
    let device = h.paired(LAN).await;
    let id = h.call("GET", "/api/auth/devices", LOOPBACK, &[], None).await.body["devices"][0]["id"].as_str().unwrap().to_string();

    // An open log stream from that device ends on revocation.
    let response = reachy_controller::app(h.state.clone())
        .oneshot(request("GET", "/api/logs/stream?source=controller", LAN, &[("cookie", &device)], None))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let mut stream = response.into_body().into_data_stream();
    assert!(stream.next().await.is_some(), "first event");

    let r = h.call("DELETE", &format!("/api/auth/devices/{id}"), LOOPBACK, &[], None).await;
    assert_eq!(r.status, StatusCode::NO_CONTENT);
    assert_eq!(h.call("GET", "/api/logs/sources", LAN, &[("cookie", &device)], None).await.status, StatusCode::UNAUTHORIZED);
    let ended = tokio::time::timeout(Duration::from_secs(2), async {
        while stream.next().await.is_some() {}
    })
    .await;
    assert!(ended.is_ok(), "stream still open after revoke");
    assert!(!h.devices_file().contains(&id));
    assert_eq!(h.call("DELETE", &format!("/api/auth/devices/{id}"), LOOPBACK, &[], None).await.status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn pair_requests_are_rate_limited() {
    let h = harness(lan()).await;
    for _ in 0..5 {
        h.pair(LAN).await;
    }
    assert_eq!(h.call("POST", "/api/auth/pair", LAN, &[], None).await.status, StatusCode::TOO_MANY_REQUESTS);
    // 20 pending in total, across peers.
    for n in 0..15 {
        h.pair(&format!("192.168.2.{}:1", n / 5 + 1)).await;
    }
    let r = h.call("POST", "/api/auth/pair", "192.168.3.1:1", &[], None).await;
    assert_eq!(r.status, StatusCode::TOO_MANY_REQUESTS);
}

#[tokio::test]
async fn trust_tailnet_off_makes_loopback_log_in() {
    let h = harness(AuthConfig { trust_loopback: false, ..lan() }).await;
    let r = h.call("GET", "/api/logs/sources", LOOPBACK, &[], None).await;
    assert_eq!(r.status, StatusCode::UNAUTHORIZED);
    // A loopback peer can't approve its own request (tailnet traffic is loopback)...
    let (id, code, pair_cookie) = h.pair(LOOPBACK).await;
    assert_eq!(h.approve(&code, LOOPBACK, &[]).await.status, StatusCode::UNAUTHORIZED);
    // ...but the CLI can, with the local key only the device's user can read.
    let key = std::fs::read_to_string(h.dir.0.join("controller.key")).unwrap();
    let key = key.trim();
    use std::os::unix::fs::PermissionsExt;
    assert_eq!(std::fs::metadata(h.dir.0.join("controller.key")).unwrap().permissions().mode() & 0o777, 0o600);
    // The key means nothing from a LAN peer.
    assert_eq!(h.approve(&code, LAN, &[("x-reachy-bridge-key", key)]).await.status, StatusCode::UNAUTHORIZED);
    assert_eq!(h.approve(&code, LOOPBACK, &[("x-reachy-bridge-key", "wrong")]).await.status, StatusCode::UNAUTHORIZED);
    assert_eq!(h.approve(&code, LOOPBACK, &[("x-reachy-bridge-key", key)]).await.status, StatusCode::OK);
    let r = h.poll(&id, LOOPBACK, &pair_cookie).await;
    let device = r.cookie("rb_device");
    assert_eq!(h.call("GET", "/api/logs/sources", LOOPBACK, &[("cookie", &device)], None).await.status, StatusCode::OK);
}

#[tokio::test]
async fn secure_flag_only_over_https() {
    let h = harness(lan()).await;
    let https = [("x-forwarded-proto", "https")];
    // From loopback (tailscale serve) X-Forwarded-Proto counts.
    let r = h.call("POST", "/api/auth/pair", LOOPBACK, &https, None).await;
    assert!(r.set_cookies()[0].contains("; Secure"));
    let (id, code, pair_cookie) = (r.body["id"].as_str().unwrap().to_string(), r.body["code"].as_str().unwrap().to_string(), r.cookie("rb_pair"));
    h.approve(&code, LOOPBACK, &[]).await;
    let r = h.call("GET", &format!("/api/auth/pair/{id}"), LOOPBACK, &[("cookie", &pair_cookie), https[0]], None).await;
    assert!(r.set_cookies().iter().any(|c| c.starts_with("rb_device=") && c.contains("; Secure")));
    // A LAN peer can't claim HTTPS.
    let r = h.call("POST", "/api/auth/pair", LAN, &https, None).await;
    assert!(!r.set_cookies()[0].contains("Secure"));
}

#[tokio::test]
async fn cross_site_state_changes_are_refused() {
    let h = harness(lan()).await;
    let device = h.paired(LAN).await;
    let (_, code, _) = h.pair(LAN2).await;
    // A page on another origin (here an app UI on another port of the robot).
    let evil = [("origin", "http://10.0.0.2:8042")];
    assert_eq!(h.approve(&code, LOOPBACK, &evil).await.status, StatusCode::FORBIDDEN);
    let fetch = [("sec-fetch-site", "same-site"), ("cookie", device.as_str())];
    assert_eq!(h.approve(&code, LAN, &fetch).await.status, StatusCode::FORBIDDEN);
    assert_eq!(h.call("POST", "/api/daemon/start", LAN, &fetch, None).await.status, StatusCode::FORBIDDEN);
    assert_eq!(h.call("DELETE", "/api/auth/devices/x", LOOPBACK, &evil, None).await.status, StatusCode::FORBIDDEN);
    // Same origin is fine.
    let same = [("origin", "http://10.0.0.2:7861"), ("sec-fetch-site", "same-origin"), ("cookie", device.as_str())];
    assert_eq!(h.approve(&code, LAN, &same).await.status, StatusCode::OK);

    // With LAN off there is no login, but another site still can't act on
    // the robot through a tailnet browser; reads and the CLI are untouched.
    let off = harness(AuthConfig::default()).await;
    assert_eq!(off.call("DELETE", "/api/auth/devices/x", LOOPBACK, &evil, None).await.status, StatusCode::FORBIDDEN);
    assert_eq!(off.call("POST", "/api/expressions/stop", LOOPBACK, &evil, None).await.status, StatusCode::FORBIDDEN);
    assert_eq!(off.call("POST", "/api/daemon/start", LOOPBACK, &[("sec-fetch-site", "cross-site")], None).await.status, StatusCode::FORBIDDEN);
    assert_eq!(off.call("GET", "/api/daemon/status", LOOPBACK, &evil, None).await.status, StatusCode::OK);
    assert_ne!(off.call("POST", "/api/expressions/stop", LOOPBACK, &[], None).await.status, StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn loopback_trust_needs_a_local_or_tailnet_host() {
    let h = harness(lan()).await;
    for host in ["127.0.0.1:7861", "localhost:7861", "[::1]:7861", "robot.tail1234.ts.net", "robot.tail1234.ts.net:10000"] {
        let r = h.call("GET", "/api/logs/sources", LOOPBACK, &[("host", host)], None).await;
        assert_eq!(r.status, StatusCode::OK, "{host}");
    }
    // A DNS-rebinding page reaches loopback with its own name as Host.
    for host in ["evil.example:7861", "127.0.0.1.evil.example", "ts.net.evil.example"] {
        let r = h.call("GET", "/api/logs/sources", LOOPBACK, &[("host", host)], None).await;
        assert_eq!(r.status, StatusCode::UNAUTHORIZED, "{host}");
    }
    let (_, code, _) = h.pair(LAN).await;
    assert_eq!(h.approve(&code, LOOPBACK, &[("host", "evil.example:7861")]).await.status, StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn ipv6_peers_are_limited_per_64_and_loopback_is_not_starved() {
    let h = harness(lan()).await;
    for n in 1..=5 {
        h.pair(&format!("[2001:db8::{n}]:1")).await;
    }
    let r = h.call("POST", "/api/auth/pair", "[2001:db8::ffff]:1", &[], None).await;
    assert_eq!(r.status, StatusCode::TOO_MANY_REQUESTS);
    // Fill the LAN pool; loopback (tailscale serve with TRUST_TAILNET=0) can still pair.
    for n in 0..15 {
        h.pair(&format!("192.168.2.{}:1", n / 5 + 1)).await;
    }
    assert_eq!(h.call("POST", "/api/auth/pair", "192.168.9.9:1", &[], None).await.status, StatusCode::TOO_MANY_REQUESTS);
    h.pair(LOOPBACK).await;
}

#[tokio::test]
async fn idle_devices_expire_and_a_bad_file_is_kept_aside() {
    let h = harness(lan()).await;
    let device = h.paired(LAN).await;
    let file = h.devices_file();
    let mut parsed: Value = serde_json::from_str(&file).unwrap();
    parsed["devices"][0]["last_seen"] = json!(1);
    std::fs::write(h.dir.0.join("devices.json"), parsed.to_string()).unwrap();
    let restart = || AppState::with_auth(Daemon::new("http://127.0.0.1:9", Duration::from_millis(200)), LogBroker::new(), h.dir.0.clone(), Duration::from_millis(10), lan());
    let restarted = Harness { state: restart(), dir: TempDir(std::env::temp_dir().join("rc-auth-unused")), robot: Default::default() };
    assert_eq!(restarted.call("GET", "/api/logs/sources", LAN, &[("cookie", &device)], None).await.status, StatusCode::UNAUTHORIZED);
    assert!(h.devices_file().contains("[]"), "expired device removed: {}", h.devices_file());

    std::fs::write(h.dir.0.join("devices.json"), "{not json").unwrap();
    let _ = restart();
    assert_eq!(std::fs::read_to_string(h.dir.0.join("devices.json.bad")).unwrap(), "{not json");
}

#[tokio::test]
async fn lan_mode_forbids_framing() {
    let h = harness(lan()).await;
    let r = h.call("GET", "/api/auth/status", LAN, &[], None).await;
    assert_eq!(r.headers["x-frame-options"], "DENY");
    assert_eq!(r.headers["content-security-policy"], "frame-ancestors 'none'");
    let off = harness(AuthConfig::default()).await;
    assert!(!off.call("GET", "/api/auth/status", LAN, &[], None).await.headers.contains_key("x-frame-options"));
}

#[tokio::test]
async fn only_the_local_key_approves_an_admin() {
    let h = harness(AuthConfig::default()).await;
    let (_, code, _) = h.pair(LOOPBACK).await;
    let admin = json!({"code": code, "name": "laptop", "role": "admin"});
    // Without LAN mode loopback is trusted, but that isn't enough for an admin.
    let r = h.call("POST", "/api/auth/approve", LOOPBACK, &[], Some(admin.clone())).await;
    assert_eq!(r.status, StatusCode::FORBIDDEN);
    let r = h.call("POST", "/api/auth/approve", LOOPBACK, &[("x-reachy-bridge-key", "wrong")], Some(admin)).await;
    assert_eq!(r.status, StatusCode::FORBIDDEN);
    let device = h.admin(LOOPBACK).await;
    let r = h.call("GET", "/api/auth/status", LOOPBACK, &[("cookie", &device)], None).await;
    assert_eq!(r.body["login_required"], true);
    assert_eq!(r.body["role"], "admin");
    assert_eq!(r.body["person"], "Andry");
    // An admin browser can't mint another admin either.
    let (_, code, _) = h.pair(LAN).await;
    let r = h.call("POST", "/api/auth/approve", LOOPBACK, &[("cookie", &device)], Some(json!({"code": code, "role": "admin"}))).await;
    assert_eq!(r.status, StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn admin_mode_makes_every_browser_log_in() {
    let h = harness(AuthConfig::default()).await;
    let admin = h.admin(LOOPBACK).await;
    // tailscale serve (loopback, tailnet Host) carries no identity any more.
    let tailnet = [("host", "robot.tail1234.ts.net")];
    assert_eq!(h.call("GET", "/api/logs/sources", LOOPBACK, &tailnet, None).await.status, StatusCode::UNAUTHORIZED);
    assert_eq!(h.call("GET", "/api/logs/sources", LOOPBACK, &[], None).await.status, StatusCode::UNAUTHORIZED);
    let r = h.call("GET", "/api/auth/status", LOOPBACK, &tailnet, None).await;
    assert_eq!((r.status, r.body["login_required"].clone(), r.body["authenticated"].clone()), (StatusCode::OK, json!(true), json!(false)));
    // A paired browser, the CLI's key, and the pairing routes still work.
    assert_eq!(h.call("GET", "/api/logs/sources", LOOPBACK, &[("cookie", &admin), tailnet[0]], None).await.status, StatusCode::OK);
    let key = h.key();
    assert_eq!(h.call("GET", "/api/logs/sources", LOOPBACK, &[("x-reachy-bridge-key", &key)], None).await.status, StatusCode::OK);
    h.pair(LOOPBACK).await;
    // The app UI proxy follows the same rule.
    let headers: HeaderMap = [("host", "robot.tail1234.ts.net:8043")].iter().map(|(k, v)| (k.parse().unwrap(), v.parse().unwrap())).collect();
    assert!(!h.state.auth.app_proxy_allowed(&headers, Some("127.0.0.1".parse().unwrap())));
    let mut with_cookie = headers.clone();
    with_cookie.insert("cookie", admin.parse().unwrap());
    assert!(h.state.auth.app_proxy_allowed(&with_cookie, Some("127.0.0.1".parse().unwrap())));
}

#[tokio::test]
async fn members_cannot_manage_devices() {
    let h = harness(AuthConfig::default()).await;
    let admin = h.admin(LOOPBACK).await;
    let member = h.paired_as(LAN, &[("cookie", &admin)], json!({"name": "phone", "person": "Sam"})).await;
    let r = h.call("GET", "/api/auth/status", LAN, &[("cookie", &member)], None).await;
    assert_eq!((r.body["role"].clone(), r.body["person"].clone()), (json!("member"), json!("Sam")));
    // A member can't approve, and sees only its own device.
    let (_, code, _) = h.pair(LAN2).await;
    assert_eq!(h.approve(&code, LAN, &[("cookie", &member)]).await.status, StatusCode::FORBIDDEN);
    let mine = h.call("GET", "/api/auth/devices", LAN, &[("cookie", &member)], None).await.body["devices"].clone();
    assert_eq!(mine.as_array().unwrap().len(), 1);
    assert_eq!((mine[0]["person"].clone(), mine[0]["current"].clone()), (json!("Sam"), json!(true)));
    let all = h.call("GET", "/api/auth/devices", LOOPBACK, &[("cookie", &admin)], None).await.body["devices"].clone();
    assert_eq!(all.as_array().unwrap().len(), 2);
    let admin_id = all.as_array().unwrap().iter().find(|d| d["role"] == "admin").unwrap()["id"].as_str().unwrap().to_string();
    // It can't revoke the admin, but it can log itself out.
    let r = h.call("DELETE", &format!("/api/auth/devices/{admin_id}"), LAN, &[("cookie", &member)], None).await;
    assert_eq!(r.status, StatusCode::FORBIDDEN);
    let member_id = mine[0]["id"].as_str().unwrap();
    let r = h.call("DELETE", &format!("/api/auth/devices/{member_id}"), LAN, &[("cookie", &member)], None).await;
    assert_eq!(r.status, StatusCode::NO_CONTENT);
    assert_eq!(h.call("GET", "/api/logs/sources", LAN, &[("cookie", &member)], None).await.status, StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn the_last_admin_is_revoked_only_from_the_robot_and_admin_mode_stays() {
    let h = harness(AuthConfig::default()).await;
    let admin = h.admin(LOOPBACK).await;
    let id = h.call("GET", "/api/auth/devices", LOOPBACK, &[("cookie", &admin)], None).await.body["devices"][0]["id"].as_str().unwrap().to_string();
    let uri = format!("/api/auth/devices/{id}");
    assert_eq!(h.call("DELETE", &uri, LOOPBACK, &[("cookie", &admin)], None).await.status, StatusCode::CONFLICT);
    let key = h.key();
    assert_eq!(h.call("DELETE", &uri, LOOPBACK, &[("x-reachy-bridge-key", &key)], None).await.status, StatusCode::NO_CONTENT);
    // No admin device left, but the robot doesn't open up again, even after a restart.
    assert_eq!(h.call("GET", "/api/logs/sources", LOOPBACK, &[], None).await.status, StatusCode::UNAUTHORIZED);
    let restarted = Harness {
        state: AppState::with_auth(Daemon::new("http://127.0.0.1:9", Duration::from_millis(200)), LogBroker::new(), h.dir.0.clone(), Duration::from_millis(10), AuthConfig::default()),
        dir: TempDir(std::env::temp_dir().join("rc-auth-unused")), robot: Default::default(),
    };
    assert_eq!(restarted.call("GET", "/api/logs/sources", LOOPBACK, &[], None).await.status, StatusCode::UNAUTHORIZED);
    // The CLI pairs a new admin.
    let key = std::fs::read_to_string(h.dir.0.join("controller.key")).unwrap();
    let (id, code, pair_cookie) = restarted.pair(LOOPBACK).await;
    let body = json!({"code": code, "role": "admin"});
    assert_eq!(restarted.call("POST", "/api/auth/approve", LOOPBACK, &[("x-reachy-bridge-key", key.trim())], Some(body)).await.status, StatusCode::OK);
    let device = restarted.poll(&id, LOOPBACK, &pair_cookie).await.cookie("rb_device");
    assert_eq!(restarted.call("GET", "/api/logs/sources", LOOPBACK, &[("cookie", &device)], None).await.status, StatusCode::OK);
}

#[tokio::test]
async fn devices_paired_before_roles_are_members() {
    let h = harness(lan()).await;
    let old = json!({"devices": [{"id": "d1", "name": "phone", "token_sha256": "00", "created": 1, "last_seen": 1, "peer": "192.168.1.20"}]});
    std::fs::create_dir_all(&h.dir.0).unwrap();
    std::fs::write(h.dir.0.join("devices.json"), old.to_string()).unwrap();
    let restarted = Harness {
        state: AppState::with_auth(Daemon::new("http://127.0.0.1:9", Duration::from_millis(200)), LogBroker::new(), h.dir.0.clone(), Duration::from_millis(10), lan()),
        dir: TempDir(std::env::temp_dir().join("rc-auth-unused")), robot: Default::default(),
    };
    assert!(!restarted.state.auth.admin_mode());
    let devices = restarted.call("GET", "/api/auth/devices", LOOPBACK, &[], None).await.body["devices"].clone();
    assert_eq!((devices[0]["person"].clone(), devices[0]["role"].clone()), (json!("phone"), json!("member")));
}

#[tokio::test]
async fn streams_without_a_device_end_when_admin_mode_starts() {
    let h = harness(AuthConfig::default()).await;
    let response = reachy_controller::app(h.state.clone())
        .oneshot(request("GET", "/api/logs/stream?source=controller", LOOPBACK, &[], None))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let mut stream = response.into_body().into_data_stream();
    assert!(stream.next().await.is_some(), "first event");
    h.admin(LOOPBACK).await;
    let ended = tokio::time::timeout(Duration::from_secs(2), async { while stream.next().await.is_some() {} }).await;
    assert!(ended.is_ok(), "stream still open after admin mode started");
}

#[tokio::test]
async fn the_first_admin_logs_out_earlier_devices() {
    let h = harness(lan()).await;
    let earlier = h.paired(LAN).await;
    assert_eq!(h.call("GET", "/api/logs/sources", LAN, &[("cookie", &earlier)], None).await.status, StatusCode::OK);
    let admin = h.admin(LAN2).await;
    assert_eq!(h.call("GET", "/api/logs/sources", LAN, &[("cookie", &earlier)], None).await.status, StatusCode::UNAUTHORIZED);
    let devices = h.call("GET", "/api/auth/devices", LAN2, &[("cookie", &admin)], None).await.body["devices"].clone();
    assert_eq!(devices.as_array().unwrap().len(), 1);
    // Later members stay.
    let member = h.paired_as(LAN, &[("cookie", &admin)], json!({"name": "phone"})).await;
    // A member can't ask for admin either.
    let (_, code, _) = h.pair(LAN).await;
    let r = h.call("POST", "/api/auth/approve", LAN, &[("cookie", &member)], Some(json!({"code": code, "role": "admin"}))).await;
    assert_eq!(r.status, StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn the_cli_approves_a_request_others_locked() {
    let h = harness(AuthConfig::default()).await;
    let (id, code, pair_cookie) = h.pair(LOOPBACK).await;
    // Anyone reaching the dashboard before an admin exists can send wrong codes.
    for _ in 0..5 {
        h.approve("000000", LOOPBACK, &[]).await;
    }
    assert_eq!(h.poll(&id, LOOPBACK, &pair_cookie).await.body["status"], "locked");
    let key = h.key();
    assert_eq!(h.approve("000000", LOOPBACK, &[("x-reachy-bridge-key", &key)]).await.status, StatusCode::BAD_REQUEST);
    let body = json!({"code": code, "role": "admin"});
    assert_eq!(h.call("POST", "/api/auth/approve", LOOPBACK, &[("x-reachy-bridge-key", &key)], Some(body)).await.status, StatusCode::OK);
    assert_eq!(h.poll(&id, LOOPBACK, &pair_cookie).await.body["status"], "approved");
}

#[tokio::test]
async fn approvals_from_before_the_admin_cant_be_collected_after() {
    let h = harness(AuthConfig::default()).await;
    // Before setup, a trusted visitor approves its own request and waits.
    let (id, code, pair_cookie) = h.pair(LOOPBACK).await;
    assert_eq!(h.approve(&code, LOOPBACK, &[]).await.status, StatusCode::OK);
    h.admin(LOOPBACK).await;
    assert_eq!(h.poll(&id, LOOPBACK, &pair_cookie).await.status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn only_an_admin_installs_apps_or_changes_the_token() {
    let h = harness(AuthConfig::default()).await;
    let admin = h.admin(LOOPBACK).await;
    let member = h.paired_as(LAN, &[("cookie", &admin)], json!({"name": "phone"})).await;
    for (method, uri) in [
        ("POST", "/api/apps/install"),
        ("POST", "/api/apps/install-private"),
        ("POST", "/api/apps/update/some_app"),
        ("POST", "/api/apps/remove/some_app"),
        ("PUT", "/api/hf/token"),
        ("DELETE", "/api/hf/token"),
        ("PUT", "/api/daemon/kinematics"),
    ] {
        let r = h.call(method, uri, LAN, &[("cookie", &member)], Some(json!({}))).await;
        assert_eq!(r.status, StatusCode::FORBIDDEN, "{method} {uri}");
        let r = h.call(method, uri, LOOPBACK, &[("cookie", &admin)], Some(json!({}))).await;
        assert_ne!(r.status, StatusCode::FORBIDDEN, "{method} {uri} as admin: {:?}", r.body);
    }
    // Reads stay open to members.
    assert_eq!(h.call("GET", "/api/logs/sources", LAN, &[("cookie", &member)], None).await.status, StatusCode::OK);
}

// Control time (#221) -------------------------------------------------------

/// Admin mode with the admin's browser (LOOPBACK) and Sam's phone (LAN).
async fn control_harness() -> (Harness, String, String) {
    let h = harness(AuthConfig::default()).await;
    let admin = h.admin(LOOPBACK).await;
    let sam = h.paired_as(LAN, &[("cookie", &admin)], json!({"name": "phone", "person": "Sam"})).await;
    (h, admin, sam)
}

impl Harness {
    async fn act(&self, peer: &str, cookie: &str) -> Reply {
        self.call("POST", "/api/expressions/stop", peer, &[("cookie", cookie)], None).await
    }

    async fn control(&self, peer: &str, cookie: &str, action: &str, body: Option<Value>) -> Reply {
        self.call("POST", &format!("/api/control/{action}"), peer, &[("cookie", cookie)], body).await
    }

    async fn view(&self, peer: &str, cookie: &str) -> Value {
        self.call("GET", "/api/control", peer, &[("cookie", cookie)], None).await.body
    }
}

#[tokio::test]
async fn without_admin_mode_nobody_needs_control() {
    let h = harness(AuthConfig::default()).await;
    assert_eq!(h.call("POST", "/api/expressions/stop", LOOPBACK, &[], None).await.status, StatusCode::OK);
    assert_eq!(h.call("GET", "/api/control", LOOPBACK, &[], None).await.body, json!({"admin_mode": false}));
}

#[tokio::test]
async fn a_person_asks_gets_time_takes_control_and_the_admin_overrides() {
    let (h, admin, sam) = control_harness().await;
    // Nobody drives without control, the admin included; reads stay open.
    let r = h.act(LAN, &sam).await;
    assert_eq!((r.status, r.body["detail"].clone()), (StatusCode::FORBIDDEN, json!("take control of the robot first")));
    assert_eq!(h.act(LOOPBACK, &admin).await.status, StatusCode::FORBIDDEN);
    assert_eq!(h.call("GET", "/api/logs/sources", LAN, &[("cookie", &sam)], None).await.status, StatusCode::OK);
    // Without a grant Sam can only ask, once.
    assert_eq!(h.control(LAN, &sam, "take", None).await.status, StatusCode::FORBIDDEN);
    assert_eq!(h.control(LAN, &sam, "take?if_free=true", None).await.status, StatusCode::FORBIDDEN);
    assert_eq!(h.control(LAN, &sam, "request", Some(json!({"minutes": 30, "note": "demo"}))).await.status, StatusCode::OK);
    assert_eq!(h.control(LAN, &sam, "request", Some(json!({"minutes": 30}))).await.status, StatusCode::CONFLICT);
    // Sam can't grant itself time; the admin sees the request and grants it.
    let grant = json!({"person": "Sam", "minutes": 30});
    assert_eq!(h.control(LAN, &sam, "grants", Some(grant.clone())).await.status, StatusCode::FORBIDDEN);
    let view = h.view(LOOPBACK, &admin).await;
    assert_eq!(view["requests"][0]["person"], "Sam");
    assert_eq!(view["requests"][0]["note"], "demo");
    assert_eq!(h.control(LOOPBACK, &admin, "grants", Some(grant)).await.status, StatusCode::OK);
    assert_eq!(h.view(LOOPBACK, &admin).await["requests"], json!([]));
    // Sam takes control and acts; the admin, watching, can't.
    assert_eq!(h.view(LAN, &sam).await["you"]["can_take"], true);
    assert_eq!(h.control(LAN, &sam, "take", None).await.status, StatusCode::OK);
    // Taking it again from the holding device is a no-op, conditional or not.
    assert_eq!(h.control(LAN, &sam, "take?if_free=true", None).await.status, StatusCode::OK);
    assert_eq!(h.act(LAN, &sam).await.status, StatusCode::OK);
    let r = h.act(LOOPBACK, &admin).await;
    assert_eq!((r.status, r.body["detail"].clone()), (StatusCode::FORBIDDEN, json!("Sam is in control of the robot")));
    let view = h.view(LOOPBACK, &admin).await;
    assert_eq!((view["holder"]["person"].clone(), view["holder"]["you"].clone()), (json!("Sam"), json!(false)));
    assert!(view["holder"]["until"].is_u64());
    // The admin overrides; Sam's next action is refused, and Sam's grant stays.
    let lost = tokio::spawn({
        let state = h.state.clone();
        let device = h.view(LAN, &sam).await;
        assert_eq!(device["holder"]["you"], true);
        async move {
            let id = state.auth.device_names().into_iter().find(|(_, name)| name == "phone").unwrap().0;
            state.control.lost(&state.auth, id).await
        }
    });
    // The dashboard's take for a robot action never takes control from someone, even for an admin.
    let r = h.control(LOOPBACK, &admin, "take?if_free=true", None).await;
    assert_eq!(r.status, StatusCode::CONFLICT);
    assert_eq!(r.body["detail"], "Sam is in control of the robot");
    assert_eq!(h.view(LAN, &sam).await["holder"]["you"], true);
    assert_eq!(h.control(LOOPBACK, &admin, "take", None).await.status, StatusCode::OK);
    assert!(tokio::time::timeout(Duration::from_secs(2), lost).await.is_ok(), "Sam's sessions didn't end");
    let r = h.act(LAN, &sam).await;
    assert_eq!(r.body["detail"], "Andry is in control of the robot");
    assert_eq!(h.control(LAN, &sam, "take", None).await.status, StatusCode::CONFLICT);
    assert_eq!(h.act(LOOPBACK, &admin).await.status, StatusCode::OK);
    // Released, Sam takes control back within the grant.
    assert_eq!(h.control(LOOPBACK, &admin, "release", None).await.status, StatusCode::OK);
    assert_eq!(h.control(LAN, &sam, "take?if_free=true", None).await.status, StatusCode::OK);
    // The CLI's key isn't held back by control.
    let key = h.key();
    assert_eq!(h.call("POST", "/api/expressions/stop", LOOPBACK, &[("x-reachy-bridge-key", &key)], None).await.status, StatusCode::OK);
}

#[tokio::test]
async fn control_ends_with_the_grant_or_its_revocation() {
    let (h, admin, sam) = control_harness().await;
    let r = h.control(LOOPBACK, &admin, "grants", Some(json!({"person": "Sam", "minutes": 1}))).await;
    let id = r.body["id"].as_str().unwrap().to_string();
    // Grants can't overlap.
    let r = h.control(LOOPBACK, &admin, "grants", Some(json!({"person": "Andry", "minutes": 5}))).await;
    assert_eq!(r.status, StatusCode::CONFLICT);
    h.control(LAN, &sam, "take", None).await;
    h.state.control.advance(61);
    assert_eq!(h.act(LAN, &sam).await.status, StatusCode::FORBIDDEN);
    assert_eq!(h.view(LAN, &sam).await["holder"], Value::Null);
    let r = h.call("DELETE", &format!("/api/control/grants/{id}"), LOOPBACK, &[("cookie", &admin)], None).await;
    assert_eq!(r.status, StatusCode::NOT_FOUND, "the ended grant is gone");
    // A grant with no end, revoked while Sam holds control.
    let r = h.control(LOOPBACK, &admin, "grants", Some(json!({"person": "Sam"}))).await;
    let id = r.body["id"].as_str().unwrap().to_string();
    assert_eq!(r.body["until"], Value::Null);
    h.control(LAN, &sam, "take", None).await;
    assert_eq!(h.act(LAN, &sam).await.status, StatusCode::OK);
    let r = h.call("DELETE", &format!("/api/control/grants/{id}"), LOOPBACK, &[("cookie", &admin)], None).await;
    assert_eq!(r.status, StatusCode::NO_CONTENT);
    assert_eq!(h.act(LAN, &sam).await.status, StatusCode::FORBIDDEN);
    // Grants survive a restart.
    h.control(LOOPBACK, &admin, "grants", Some(json!({"person": "Sam", "minutes": 60}))).await;
    assert!(std::fs::read_to_string(h.dir.0.join("control.json")).unwrap().contains("Sam"));
}

#[tokio::test]
async fn a_silent_holder_steps_away_and_comes_back() {
    let (h, admin, sam) = control_harness().await;
    h.control(LOOPBACK, &admin, "grants", Some(json!({"person": "Sam", "minutes": 60}))).await;
    h.control(LAN, &sam, "take", None).await;
    // The phone's screen goes off: no requests for 10 s.
    h.state.control.advance(11);
    assert_eq!(h.view(LOOPBACK, &admin).await["holder"], Value::Null);
    // It comes back within the grant and has control again.
    assert_eq!(h.view(LAN, &sam).await["holder"]["you"], true);
    assert_eq!(h.act(LAN, &sam).await.status, StatusCode::OK);
    // Unless the admin took it meanwhile.
    h.state.control.advance(11);
    h.control(LOOPBACK, &admin, "take", None).await;
    assert_eq!(h.view(LAN, &sam).await["holder"]["person"], "Andry");
}

#[tokio::test]
async fn refusing_or_cancelling_a_request_starts_a_cooldown() {
    let (h, admin, sam) = control_harness().await;
    h.control(LAN, &sam, "request", Some(json!({}))).await;
    let r = h.call("POST", "/api/control/requests/Sam/refuse", LAN, &[("cookie", &sam)], None).await;
    assert_eq!(r.status, StatusCode::FORBIDDEN);
    let r = h.call("POST", "/api/control/requests/Sam/refuse", LOOPBACK, &[("cookie", &admin)], None).await;
    assert_eq!(r.status, StatusCode::NO_CONTENT);
    assert_eq!(h.control(LAN, &sam, "request", Some(json!({}))).await.status, StatusCode::TOO_MANY_REQUESTS);
    assert!(h.view(LAN, &sam).await["you"]["cooldown_until"].is_u64());
    h.state.control.advance(601);
    assert_eq!(h.control(LAN, &sam, "request", Some(json!({}))).await.status, StatusCode::OK);
    let r = h.call("DELETE", "/api/control/request", LAN, &[("cookie", &sam)], None).await;
    assert_eq!(r.status, StatusCode::NO_CONTENT);
    assert_eq!(h.control(LAN, &sam, "request", Some(json!({}))).await.status, StatusCode::TOO_MANY_REQUESTS);
    // A person with no paired device can't be granted time.
    let r = h.control(LOOPBACK, &admin, "grants", Some(json!({"person": "Nobody"}))).await;
    assert_eq!(r.status, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn control_details() {
    let (h, admin, sam) = control_harness().await;
    // Favorites are a preference, not a robot action.
    let r = h.call("PUT", "/api/apps/favorites", LAN, &[("cookie", &sam)], Some(json!({"favorites": []}))).await;
    assert_ne!(r.status, StatusCode::FORBIDDEN);
    // A drive WebSocket upgrade needs control like any action.
    let upgrade = [("cookie", sam.as_str()), ("connection", "upgrade"), ("upgrade", "websocket"), ("sec-websocket-version", "13"), ("sec-websocket-key", "dGhlIHNhbXBsZSBub25jZQ==")];
    assert_eq!(h.call("GET", "/api/drive/ws", LAN, &upgrade, None).await.status, StatusCode::FORBIDDEN);
    // A slot can't start absurdly far ahead (no overflow).
    let r = h.control(LOOPBACK, &admin, "grants", Some(json!({"person": "Sam", "minutes": 10, "from": u64::MAX}))).await;
    assert_eq!(r.status, StatusCode::OK);
    assert!(r.body["until"].as_u64().unwrap() > r.body["from"].as_u64().unwrap());
    // An admin away too long doesn't silently get control back.
    h.control(LOOPBACK, &admin, "take", None).await;
    h.state.control.advance(11);
    assert_eq!(h.view(LAN, &sam).await["holder"], Value::Null);
    h.state.control.advance(300);
    assert_eq!(h.view(LOOPBACK, &admin).await["holder"], Value::Null);
    // The same with no request at all while the admin was gone.
    h.control(LOOPBACK, &admin, "take", None).await;
    h.state.control.advance(400);
    assert_eq!(h.view(LOOPBACK, &admin).await["holder"], Value::Null);
    // A short absence is forgiven.
    h.control(LOOPBACK, &admin, "take", None).await;
    h.state.control.advance(60);
    assert_eq!(h.view(LOOPBACK, &admin).await["holder"]["you"], true);
}

#[tokio::test]
async fn devices_paired_before_people_use_their_name() {
    let h = harness(lan()).await;
    // A member paired in LAN mode before #221, then the admin.
    let old = h.paired(LAN).await;
    let mut file: Value = serde_json::from_str(&h.devices_file()).unwrap();
    file["devices"][0]["person"] = json!("");
    std::fs::write(h.dir.0.join("devices.json"), file.to_string()).unwrap();
    let restarted = Harness {
        state: AppState::with_auth(Daemon::new("http://127.0.0.1:9", Duration::from_millis(200)), LogBroker::new(), h.dir.0.clone(), Duration::from_millis(10), lan()),
        dir: TempDir(std::env::temp_dir().join("rc-auth-unused")), robot: Default::default(),
    };
    let r = restarted.call("GET", "/api/auth/status", LAN, &[("cookie", &old)], None).await;
    assert_eq!(r.body["person"], "phone");
}

// Listen and talk (#219) -----------------------------------------------------

/// A stand-in audio gateway: one session "s1", always connected; records
/// deletes. It signs its replies with `key` (None: a squatter that can't).
async fn fake_gateway(deleted: Arc<AtomicU64>, key: Option<String>) -> String {
    use axum::routing::{delete, post};
    let open = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let app = Router::new()
        .route(
            "/status",
            get({
                let open = open.clone();
                move || async move {
                    let session = if open.load(Ordering::SeqCst) {
                        json!({"id": "s1", "connected": true})
                    } else {
                        Value::Null
                    };
                    Json(json!({"session": session}))
                }
            }),
        )
        .route(
            "/session",
            post({
                let open = open.clone();
                move || async move {
                    open.store(true, Ordering::SeqCst);
                    Json(json!({"id": "s1", "offer": "v=0\r\n"}))
                }
            }),
        )
        .route(
            "/session/s1/answer",
            post(|| async { StatusCode::NO_CONTENT }),
        )
        .route(
            "/session/s1",
            delete({
                let open = open.clone();
                move || async move {
                    open.store(false, Ordering::SeqCst);
                    deleted.fetch_add(1, Ordering::SeqCst);
                    StatusCode::NO_CONTENT
                }
            }),
        )
        .layer(axum::middleware::from_fn(move |request: Request<Body>, next: axum::middleware::Next| {
            let key = key.clone();
            async move {
                let auth = request.headers().get(audio::AUTH_HEADER).and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
                let mut response = next.run(request).await;
                if let (Some(key), Some(nonce)) = (key, auth.split('.').nth(1)) {
                    response.headers_mut().insert(audio::PROOF_HEADER, audio::sign(&key, "reply", nonce).parse().unwrap());
                }
                response
            }
        }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    url
}

#[test]
fn signs_like_the_gateway() {
    // Vectors from runtime/.../audio_gateway.py's sign().
    assert_eq!(audio::sign("key", "request", "00"), "3d015b8f1153a6c9583a8918449da7ef14f83a80453c525fc6ab27a28e5ebdde");
    assert_eq!(audio::sign(&"k".repeat(70), "reply", "ab"), "36dfa5b9b523a5c76a47b81281bdf031eb86d84df0e2183c621c4f91e8043147");
}

#[tokio::test]
async fn a_squatter_on_the_gateway_port_is_not_believed() {
    let h = harness(AuthConfig::default()).await;
    h.robot.store(ROBOT_ON, Ordering::SeqCst);
    let deleted = Arc::new(AtomicU64::new(0));
    h.state.audio.set_url(&fake_gateway(deleted, None).await);
    let r = h.call("POST", "/api/audio/session", LOOPBACK, &[], Some(json!({"listen": true, "talk": true}))).await;
    assert_eq!(r.status, StatusCode::SERVICE_UNAVAILABLE, "{:?}", r.body);
    assert_eq!(h.call("GET", "/api/audio", LOOPBACK, &[], None).await.body["session"], Value::Null);
}

#[tokio::test]
async fn voice_goes_with_the_robot() {
    let h = harness(AuthConfig::default()).await;
    let deleted = Arc::new(AtomicU64::new(0));
    let key = std::fs::read_to_string(h.state.state_dir.join(audio::KEY_FILE)).unwrap().trim().to_string();
    h.state.audio.set_url(&fake_gateway(deleted.clone(), Some(key)).await);
    let open = json!({"listen": true, "talk": false});
    let refused = |power, status| {
        let (h, open) = (&h, open.clone());
        async move {
            h.robot.store(power, Ordering::SeqCst);
            let r = h.call("POST", "/api/audio/session", LOOPBACK, &[], Some(open)).await;
            assert_eq!(r.status, status, "power {power}: {:?}", r.body);
            r.body["detail"].as_str().unwrap_or_default().to_string()
        }
    };
    assert!(refused(ROBOT_OFF, StatusCode::CONFLICT).await.contains("Turn the robot on"));
    assert!(refused(ROBOT_STARTING, StatusCode::CONFLICT).await.contains("still starting"));
    assert!(refused(ROBOT_CRASHED, StatusCode::CONFLICT).await.contains("Turn the robot on"));
    refused(ROBOT_SILENT, StatusCode::SERVICE_UNAVAILABLE).await;
    // Turning the robot off (or its backend crashing) ends a session within
    // a check; a daemon that stops answering within a few.
    for (power, within) in [(ROBOT_OFF, 4), (ROBOT_CRASHED, 4), (ROBOT_SILENT, 9)] {
        let before = deleted.load(Ordering::SeqCst);
        h.robot.store(ROBOT_ON, Ordering::SeqCst);
        assert_eq!(h.call("POST", "/api/audio/session", LOOPBACK, &[], Some(open.clone())).await.status, StatusCode::OK);
        h.robot.store(power, Ordering::SeqCst);
        tokio::time::timeout(Duration::from_secs(within), async {
            while deleted.load(Ordering::SeqCst) == before {
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
        })
        .await
        .unwrap_or_else(|_| panic!("the session outlived power {power}"));
        assert_eq!(h.call("GET", "/api/audio", LOOPBACK, &[], None).await.body["session"], Value::Null);
    }
}

#[tokio::test]
async fn only_the_holder_listens_and_talks_and_loses_it_with_control() {
    let (h, admin, sam) = control_harness().await;
    h.robot.store(ROBOT_ON, Ordering::SeqCst);
    let deleted = Arc::new(AtomicU64::new(0));
    let key = std::fs::read_to_string(h.state.state_dir.join(audio::KEY_FILE)).unwrap().trim().to_string();
    h.state.audio.set_url(&fake_gateway(deleted.clone(), Some(key)).await);
    let open = json!({"listen": true, "talk": true});
    let r = h
        .call(
            "POST",
            "/api/audio/session",
            LAN,
            &[("cookie", &sam)],
            Some(open.clone()),
        )
        .await;
    assert_eq!(r.status, StatusCode::FORBIDDEN, "no control, no audio");
    h.control(
        LOOPBACK,
        &admin,
        "grants",
        Some(json!({"person": "Sam", "minutes": 30})),
    )
    .await;
    h.control(LAN, &sam, "take", None).await;
    let r = h
        .call(
            "POST",
            "/api/audio/session",
            LAN,
            &[("cookie", &sam)],
            Some(open),
        )
        .await;
    assert_eq!(
        (r.status, r.body["id"].clone()),
        (StatusCode::OK, json!("s1"))
    );
    // Everyone sees the mic is live; only Sam's browser gets the session id.
    let mine = h
        .call("GET", "/api/audio", LAN, &[("cookie", &sam)], None)
        .await
        .body;
    assert_eq!(
        (
            mine["session"]["you"].clone(),
            mine["session"]["id"].clone()
        ),
        (json!(true), json!("s1"))
    );
    let theirs = h
        .call("GET", "/api/audio", LOOPBACK, &[("cookie", &admin)], None)
        .await
        .body;
    assert_eq!(
        (
            theirs["session"]["person"].clone(),
            theirs["session"]["id"].clone()
        ),
        (json!("Sam"), Value::Null)
    );
    assert_eq!(theirs["session"]["ref"], r.body["ref"], "the public ref, for telling sessions apart");
    // Only Sam answers it.
    let answer = json!({"sdp": "v=0\r\n"});
    let r = h
        .call(
            "POST",
            "/api/audio/session/s1/answer",
            LAN,
            &[("cookie", &sam)],
            Some(answer),
        )
        .await;
    assert_eq!(r.status, StatusCode::OK);
    // The admin takes over: Sam's audio ends at once.
    h.control(LOOPBACK, &admin, "take", None).await;
    tokio::time::timeout(Duration::from_secs(3), async {
        while deleted.load(Ordering::SeqCst) == 0 {
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    })
    .await
    .expect("Sam's session wasn't closed");
    assert_eq!(
        h.call("GET", "/api/audio", LAN, &[("cookie", &sam)], None)
            .await
            .body["session"],
        Value::Null
    );
}
