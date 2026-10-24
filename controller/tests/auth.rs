//! LAN login (#141), in-process against a minimal fake daemon.
//!
//! Peers are simulated by inserting `ConnectInfo<SocketAddr>` into each
//! request's extensions, which is where `into_make_service_with_connect_info`
//! puts the real socket address in production.

use std::net::SocketAddr;
use std::time::Duration;

use axum::body::Body;
use axum::extract::ConnectInfo;
use axum::http::{HeaderMap, Request, StatusCode};
use axum::routing::get;
use axum::{Json, Router};
use futures_util::StreamExt;
use reachy_controller::auth::AuthConfig;
use reachy_controller::daemon::Daemon;
use reachy_controller::logs::LogBroker;
use reachy_controller::state::{AppState, Shared};
use serde_json::{json, Value};
use tower::ServiceExt;

const LOOPBACK: &str = "127.0.0.1:50000";
const LAN: &str = "192.168.1.50:50000";
const LAN2: &str = "192.168.1.51:50000";

async fn fake_daemon() -> String {
    let app = Router::new().route(
        "/api/daemon/status",
        get(|| async { Json(json!({"state": "stopped", "backend_status": null, "error": null})) }),
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
    let url = fake_daemon().await;
    let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
    let dir = std::env::temp_dir().join(format!("rc-auth-{}-{nanos}", std::process::id()));
    let state = AppState::with_auth(
        Daemon::new(&url, Duration::from_secs(2)),
        LogBroker::new(),
        dir.clone(),
        Duration::from_millis(10),
        config,
    );
    Harness { state, dir: TempDir(dir) }
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
    assert_eq!(r.body, json!({"lan": false, "authenticated": true, "device": null}));
    assert_eq!(h.call("POST", "/api/auth/pair", LAN, &[], None).await.status, StatusCode::CONFLICT);
    assert!(!h.dir.0.join("controller.key").exists(), "no local key outside LAN mode");
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
    assert_eq!(r.body, json!({"lan": true, "authenticated": false, "device": null}));
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
    let restarted = Harness { state, dir: TempDir(std::env::temp_dir().join("rc-auth-unused")) };
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

    // With LAN off only the new auth routes are checked.
    let off = harness(AuthConfig::default()).await;
    assert_eq!(off.call("DELETE", "/api/auth/devices/x", LOOPBACK, &evil, None).await.status, StatusCode::FORBIDDEN);
    assert_ne!(off.call("POST", "/api/expressions/stop", LOOPBACK, &evil, None).await.status, StatusCode::FORBIDDEN);
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
    let restarted = Harness { state: restart(), dir: TempDir(std::env::temp_dir().join("rc-auth-unused")) };
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
