//! The setup page (#135): the one-time code, its lockout, the setup session,
//! what the page's port serves, and pairing the first admin.
//!
//! The setup listener is a real socket (`SETUP_PORT=0`), driven with reqwest;
//! the controller's own API is called in-process with a simulated peer.

use std::io::Write;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use axum::body::Body;
use axum::extract::ConnectInfo;
use axum::http::{Request, StatusCode};
use axum::routing::get;
use axum::{Json, Router};
use reachy_controller::auth::AuthConfig;
use reachy_controller::daemon::Daemon;
use reachy_controller::logs::LogBroker;
use reachy_controller::state::{AppState, Shared};
use serde_json::{Value, json};
use tower::ServiceExt;

const LOOPBACK: &str = "127.0.0.1:50000";
const LAN: &str = "192.168.1.50:50000";

/// Everything the controller logs while these tests run.
fn log() -> &'static Arc<Mutex<Vec<u8>>> {
    static LOG: OnceLock<Arc<Mutex<Vec<u8>>>> = OnceLock::new();
    LOG.get_or_init(|| {
        let buffer = Arc::new(Mutex::new(Vec::new()));
        let sink = buffer.clone();
        let writer = move || Capture(sink.clone());
        let _ = tracing_subscriber::fmt()
            .without_time()
            .with_max_level(tracing::Level::TRACE)
            .with_writer(writer)
            .try_init();
        buffer
    })
}

struct Capture(Arc<Mutex<Vec<u8>>>);

impl Write for Capture {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

struct Harness {
    state: Shared,
    dir: std::path::PathBuf,
}

impl Drop for Harness {
    fn drop(&mut self) {
        self.state.setup.close("test over");
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

async fn harness() -> Harness {
    log();
    // SAFETY: every test sets the same value; nothing reads it concurrently in a conflicting way.
    unsafe { std::env::set_var("SETUP_PORT", "0") };
    let app = Router::new().route(
        "/api/daemon/status",
        get(|| async { Json(json!({"state": "stopped", "backend_status": null})) }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    static NEXT: AtomicU64 = AtomicU64::new(0);
    let dir = std::env::temp_dir().join(format!(
        "rc-setup-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    let state = AppState::with_auth(
        Daemon::new(&url, Duration::from_secs(2)),
        LogBroker::new(),
        dir.clone(),
        Duration::from_millis(10),
        AuthConfig::default(),
    );
    Harness { state, dir }
}

impl Harness {
    fn key(&self) -> String {
        std::fs::read_to_string(self.dir.join("controller.key"))
            .unwrap()
            .trim()
            .to_string()
    }

    /// A call to the controller's own API from `peer`.
    async fn api(
        &self,
        method: &str,
        uri: &str,
        peer: &str,
        headers: &[(&str, &str)],
        body: Option<Value>,
    ) -> (StatusCode, Value) {
        let mut builder = Request::builder()
            .method(method)
            .uri(uri)
            .header("host", "127.0.0.1:7861");
        for (k, v) in headers {
            builder = builder.header(*k, *v);
        }
        if body.is_some() {
            builder = builder.header("content-type", "application/json");
        }
        let mut request = builder
            .body(body.map_or_else(Body::empty, |b| Body::from(b.to_string())))
            .unwrap();
        request
            .extensions_mut()
            .insert(ConnectInfo(peer.parse::<SocketAddr>().unwrap()));
        let response = reachy_controller::app(self.state.clone())
            .oneshot(request)
            .await
            .unwrap();
        let status = response.status();
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        (
            status,
            serde_json::from_slice(&bytes).unwrap_or(Value::Null),
        )
    }

    /// `reachy-bridge setup`: (code, setup page base URL).
    async fn start(&self) -> (String, String) {
        let key = self.key();
        let (status, body) = self
            .api(
                "POST",
                "/api/setup/start",
                LOOPBACK,
                &[("x-reachy-bridge-key", &key)],
                None,
            )
            .await;
        assert_eq!(status, StatusCode::OK, "{body:?}");
        assert_eq!(body["expires_in"], 900);
        (
            body["code"].as_str().unwrap().to_string(),
            format!("http://127.0.0.1:{}", body["port"]),
        )
    }
}

fn client() -> reqwest::Client {
    reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .unwrap()
}

async fn login(page: &str, code: &str) -> reqwest::Response {
    client()
        .post(format!("{page}/api/setup/login"))
        .json(&json!({"code": code}))
        .send()
        .await
        .unwrap()
}

/// The `rb_setup=...` pair from a login response.
fn session(response: &reqwest::Response) -> String {
    let line = response
        .headers()
        .get_all("set-cookie")
        .iter()
        .map(|v| v.to_str().unwrap())
        .find(|c| c.starts_with("rb_setup="))
        .expect("setup cookie");
    assert!(
        line.contains("HttpOnly") && line.contains("SameSite=Strict"),
        "{line}"
    );
    line.split(';').next().unwrap().to_string()
}

fn wrong(code: &str) -> String {
    format!("{:06}", (code.parse::<u32>().unwrap() + 1) % 1_000_000)
}

#[tokio::test]
async fn only_the_local_key_opens_the_page() {
    let h = harness().await;
    for (peer, headers) in [
        (LOOPBACK, vec![]),
        (LAN, vec![]),
        (LOOPBACK, vec![("x-reachy-bridge-key", "nope")]),
    ] {
        let (status, _) = h
            .api("POST", "/api/setup/start", peer, &headers, None)
            .await;
        assert_eq!(status, StatusCode::FORBIDDEN, "{peer} {headers:?}");
    }
    assert!(!h.state.setup.is_open());
    let (code, _) = h.start().await;
    assert_eq!(code.len(), 6);
    assert!(code.bytes().all(|b| b.is_ascii_digit()));
}

#[tokio::test]
async fn the_code_is_never_logged() {
    let h = harness().await;
    let (code, page) = h.start().await;
    assert_eq!(
        login(&page, &wrong(&code)).await.status(),
        StatusCode::BAD_REQUEST
    );
    assert_eq!(login(&page, &code).await.status(), StatusCode::OK);
    let (code2, _) = h.start().await;
    let logged = String::from_utf8_lossy(&log().lock().unwrap()).to_string();
    assert!(
        logged.contains("setup: page open"),
        "the setup log lines are captured"
    );
    for c in [&code, &code2] {
        assert!(!logged.contains(c.as_str()), "code {c} in the log");
    }
}

#[tokio::test]
async fn five_wrong_codes_lock_the_page_until_the_terminal_issues_a_new_one() {
    let h = harness().await;
    let (code, page) = h.start().await;
    for left in (1..5).rev() {
        let r = login(&page, &wrong(&code)).await;
        assert_eq!(r.status(), StatusCode::BAD_REQUEST);
        assert!(
            r.text()
                .await
                .unwrap()
                .contains(&format!("{left} tries left"))
        );
    }
    assert_eq!(
        login(&page, &wrong(&code)).await.status(),
        StatusCode::LOCKED
    );
    // Locked: even the right code is refused.
    assert_eq!(login(&page, &code).await.status(), StatusCode::LOCKED);
    let status: Value = client()
        .get(format!("{page}/api/setup/status"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(status["locked"], true);

    // A new code from the terminal unlocks it; the old one is dead.
    let (code2, page2) = h.start().await;
    assert_eq!(page2, page, "same page, new code");
    assert_eq!(
        login(&page, &code).await.status() == StatusCode::OK,
        code == code2
    );
    let r = login(&page, &code2).await;
    assert_eq!(r.status(), StatusCode::OK);
}

#[tokio::test]
async fn the_code_works_once_and_the_session_gates_the_setup_routes() {
    let h = harness().await;
    let (code, page) = h.start().await;
    let c = client();
    assert_eq!(
        c.get(format!("{page}/api/setup/check"))
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        c.post(format!("{page}/api/setup/finish"))
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );

    let r = login(&page, &code).await;
    assert_eq!(r.status(), StatusCode::OK);
    let cookie = session(&r);
    // Single use: a second browser can't log in with it.
    assert_eq!(login(&page, &code).await.status(), StatusCode::CONFLICT);
    let status: Value = c
        .get(format!("{page}/api/setup/status"))
        .header("cookie", &cookie)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(status["authenticated"], true);
    assert!(
        status["expires_in"].as_u64().unwrap() > 3000,
        "an hour once used"
    );
    let status: Value = c
        .get(format!("{page}/api/setup/status"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(status["authenticated"], false);
    // A forged cookie is not a session.
    let r = c
        .post(format!("{page}/api/setup/finish"))
        .header("cookie", "rb_setup=AAAA")
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::UNAUTHORIZED);

    // A new code from the terminal ends the old session.
    h.start().await;
    let r = c
        .post(format!("{page}/api/setup/finish"))
        .header("cookie", &cookie)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn the_page_serves_only_setup() {
    let h = harness().await;
    let (code, page) = h.start().await;
    let cookie = session(&login(&page, &code).await);
    let c = client();
    // No controller API, even with the session; no app proxy, no daemon.
    for path in [
        "/api/daemon/status",
        "/api/auth/status",
        "/api/auth/pair",
        "/api/apps/list",
        "/api/setup/start",
        "/telemetry/stream",
    ] {
        let r = c
            .get(format!("{page}{path}"))
            .header("cookie", &cookie)
            .send()
            .await
            .unwrap();
        assert_eq!(r.status(), StatusCode::NOT_FOUND, "{path}");
        let r = c
            .post(format!("{page}{path}"))
            .header("cookie", &cookie)
            .send()
            .await
            .unwrap();
        assert_eq!(r.status(), StatusCode::NOT_FOUND, "POST {path}");
    }
    // Without the session too: the page doesn't even say they need one.
    let r = c.get(format!("{page}/api/daemon/status")).send().await.unwrap();
    assert_eq!(r.status(), StatusCode::NOT_FOUND);
    let r = c.get(format!("{page}/")).send().await.unwrap();
    assert_eq!(r.headers()["x-frame-options"], "DENY");
    // Another site can't drive it.
    let r = c
        .post(format!("{page}/api/setup/login"))
        .header("origin", "http://evil.example")
        .json(&json!({"code": code}))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::FORBIDDEN);
    let r = c
        .post(format!("{page}/api/setup/finish"))
        .header("cookie", &cookie)
        .header("sec-fetch-site", "same-site")
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn the_setup_browser_approves_the_dashboards_code_as_the_admin() {
    let h = harness().await;
    // A browser paired before the admin is logged out when the admin pairs.
    let (status, earlier) = h.api("POST", "/api/auth/pair", LAN, &[], None).await;
    assert_eq!(status, StatusCode::OK);
    let key = h.key();
    let (status, _) = h
        .api(
            "POST",
            "/api/auth/approve",
            LOOPBACK,
            &[("x-reachy-bridge-key", &key)],
            Some(json!({"code": earlier["code"]})),
        )
        .await;
    assert_eq!(status, StatusCode::OK);

    // The dashboard asks for a code...
    let mut request = Request::builder()
        .method("POST")
        .uri("/api/auth/pair")
        .header("host", "127.0.0.1:7861")
        .body(Body::empty())
        .unwrap();
    request
        .extensions_mut()
        .insert(ConnectInfo(LAN.parse::<SocketAddr>().unwrap()));
    let response = reachy_controller::app(h.state.clone())
        .oneshot(request)
        .await
        .unwrap();
    let pair_cookie = response.headers()["set-cookie"]
        .to_str()
        .unwrap()
        .split(';')
        .next()
        .unwrap()
        .to_string();
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let pair: Value = serde_json::from_slice(&bytes).unwrap();

    // ...and the setup page approves it as the admin.
    let (code, page) = h.start().await;
    let cookie = session(&login(&page, &code).await);
    let c = client();
    let r = c
        .post(format!("{page}/api/setup/admin"))
        .json(&json!({"code": pair["code"], "person": "Andry"}))
        .send()
        .await
        .unwrap();
    assert_eq!(
        r.status(),
        StatusCode::UNAUTHORIZED,
        "needs the setup session"
    );
    let r = c
        .post(format!("{page}/api/setup/admin"))
        .header("cookie", &cookie)
        .json(&json!({"code": "000000", "person": "Andry"}))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::BAD_REQUEST);
    let r = c
        .post(format!("{page}/api/setup/admin"))
        .header("cookie", &cookie)
        .json(&json!({"code": pair["code"], "person": "Andry", "name": "laptop"}))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::OK);
    let body: Value = r.json().await.unwrap();
    assert_eq!(body["role"], "admin");

    let id = pair["id"].as_str().unwrap();
    let (status, body) = h
        .api(
            "GET",
            &format!("/api/auth/pair/{id}"),
            LAN,
            &[("cookie", &pair_cookie)],
            None,
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["status"], "approved");
    assert!(h.state.auth.admin_mode());
    let devices = std::fs::read_to_string(h.dir.join("devices.json")).unwrap();
    assert!(
        devices.contains("\"role\":\"admin\"") || devices.contains("\"role\": \"admin\""),
        "{devices}"
    );
    assert!(devices.contains("Andry"));
    assert_eq!(
        devices.matches("token_sha256").count(),
        1,
        "the earlier device is logged out"
    );
}

#[tokio::test]
async fn finishing_closes_the_page() {
    let h = harness().await;
    let (code, page) = h.start().await;
    let cookie = session(&login(&page, &code).await);
    let r = client()
        .post(format!("{page}/api/setup/finish"))
        .header("cookie", &cookie)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::OK);
    assert!(!h.state.setup.is_open());
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert!(
        client()
            .get(format!("{page}/api/setup/status"))
            .send()
            .await
            .is_err(),
        "the listener is gone"
    );
    // The terminal can open it again.
    let (_, page2) = h.start().await;
    assert!(
        client()
            .get(format!("{page2}/api/setup/status"))
            .send()
            .await
            .unwrap()
            .status()
            .is_success()
    );
}

#[tokio::test]
async fn stopping_from_the_terminal_closes_the_page() {
    let h = harness().await;
    let (_, page) = h.start().await;
    let (status, _) = h.api("POST", "/api/setup/stop", LAN, &[], None).await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    let key = h.key();
    let (status, _) = h
        .api(
            "POST",
            "/api/setup/stop",
            LOOPBACK,
            &[("x-reachy-bridge-key", &key)],
            None,
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert!(
        client()
            .get(format!("{page}/api/setup/status"))
            .send()
            .await
            .is_err()
    );
}

#[tokio::test]
async fn the_device_check_reports_the_runtimes_json() {
    let h = harness().await;
    let script = h.dir.join("runtime.sh");
    std::fs::create_dir_all(&h.dir).unwrap();
    std::fs::write(&script, "#!/bin/sh\n[ \"$1 $2\" = \"--doctor --json\" ] || exit 9\necho '[{\"name\": \"OS\", \"status\": \"pass\", \"detail\": \"Linux\"}]'\nexit 1\n").unwrap();
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
    // SAFETY: only this test reads it, through the check route.
    unsafe { std::env::set_var("REACHY_BRIDGE_RUNTIME", &script) };
    let (code, page) = h.start().await;
    let cookie = session(&login(&page, &code).await);
    let r = client()
        .get(format!("{page}/api/setup/check"))
        .header("cookie", &cookie)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::OK);
    let body: Value = r.json().await.unwrap();
    assert_eq!(body["results"][0]["status"], "pass");
}

#[tokio::test]
async fn app_proxies_never_forward_the_setup_cookie() {
    let mut headers = axum::http::HeaderMap::new();
    headers.insert("cookie", "rb_setup=abc; theme=dark".parse().unwrap());
    reachy_controller::auth::strip_credentials(&mut headers);
    assert_eq!(headers["cookie"], "theme=dark");
    let mut headers = axum::http::HeaderMap::new();
    headers.append("set-cookie", "rb_setup=evil; Path=/".parse().unwrap());
    headers.append("set-cookie", "app=1".parse().unwrap());
    reachy_controller::auth::strip_credential_cookies(&mut headers);
    assert_eq!(headers.get_all("set-cookie").iter().count(), 1);
}
