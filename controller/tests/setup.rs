//! The setup page (#135): the one-time code, its lockout, the setup session,
//! what the page's port serves, and pairing the first admin.
//!
//! The setup listener is a real socket (`SETUP_PORT=0`), driven with reqwest;
//! the controller's own API is called in-process with a simulated peer.

use std::io::Write;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, Once, OnceLock};
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

/// A stand-in for `reachy-bridge-runtime --doctor --json`.
fn runtime_script() -> std::path::PathBuf {
    std::env::temp_dir().join(format!("rc-setup-runtime-{}.sh", std::process::id()))
}

/// The environment, set once before any test starts the page.
fn environment() {
    static ONCE: Once = Once::new();
    ONCE.call_once(|| {
        let script = runtime_script();
        std::fs::write(&script, "#!/bin/sh\n[ \"$1 $2\" = \"--doctor --json\" ] || exit 9\necho '[{\"name\": \"OS\", \"status\": \"pass\", \"detail\": \"Linux\"}]'\nexit 1\n").unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
        // SAFETY: set once, before any test reads them.
        unsafe {
            std::env::set_var("SETUP_PORT", "0");
            std::env::set_var("REACHY_BRIDGE_RUNTIME", &script);
        }
    });
}

/// Wait until the setup listener at `page` stops answering.
async fn gone(page: &str) -> bool {
    for _ in 0..100 {
        if client().get(format!("{page}/api/setup/status")).send().await.is_err() {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    false
}

async fn harness() -> Harness {
    let app = Router::new().route(
        "/api/daemon/status",
        get(|| async { Json(json!({"state": "stopped", "backend_status": null})) }),
    );
    harness_with(app).await
}

/// A daemon that powers on and off; after a start without the wake-up move
/// the motors report `mode` (limp, `disabled`, on the real robot:
/// daemon-contract.md). `calls` records each POST; `refuse_start` answers
/// starts with that status instead.
#[derive(Clone)]
struct FakeRobot {
    on: Arc<Mutex<bool>>,
    mode: Arc<Mutex<&'static str>>,
    refuse_start: Arc<Mutex<Option<u16>>>,
    calls: Arc<Mutex<Vec<String>>>,
}

impl Default for FakeRobot {
    fn default() -> Self {
        Self { on: Default::default(), mode: Arc::new(Mutex::new("disabled")), refuse_start: Default::default(), calls: Default::default() }
    }
}

impl FakeRobot {
    fn router(&self) -> Router {
        use axum::extract::{Path, RawQuery};
        use axum::routing::post;
        let (a, b, c, d) = (self.clone(), self.clone(), self.clone(), self.clone());
        Router::new()
            .route(
                "/api/daemon/status",
                get(move || {
                    let (on, mode) = (*a.on.lock().unwrap(), *a.mode.lock().unwrap());
                    async move {
                        Json(if on {
                            json!({"state": "running", "backend_status": {"motor_control_mode": mode}})
                        } else {
                            json!({"state": "not_initialized", "backend_status": null})
                        })
                    }
                }),
            )
            .route(
                "/api/daemon/start",
                post(move |RawQuery(q): RawQuery| {
                    b.calls.lock().unwrap().push(format!("start?{}", q.unwrap_or_default()));
                    let refused = *b.refuse_start.lock().unwrap();
                    if refused.is_none() {
                        *b.on.lock().unwrap() = true;
                    }
                    async move {
                        match refused {
                            Some(code) => (StatusCode::from_u16(code).unwrap(), Json(json!({"detail": "Daemon is busy."}))),
                            None => (StatusCode::OK, Json(json!({"job_id": "1"}))),
                        }
                    }
                }),
            )
            .route(
                "/api/daemon/stop",
                post(move |RawQuery(q): RawQuery| {
                    c.calls.lock().unwrap().push(format!("stop?{}", q.unwrap_or_default()));
                    *c.on.lock().unwrap() = false;
                    async { Json(json!({"job_id": "2"})) }
                }),
            )
            .route(
                "/api/motors/set_mode/{mode}",
                post(move |Path(mode): Path<String>| {
                    d.calls.lock().unwrap().push(format!("set_mode/{mode}"));
                    if mode == "disabled" {
                        *d.mode.lock().unwrap() = "disabled";
                    }
                    async { Json(json!({"status": "ok"})) }
                }),
            )
            .route("/api/apps/current-app-status", get(|| async { Json(Value::Null) }))
            .route("/api/state/full", get(|| async { Json(json!({"head_pose": {"pitch": 0.1}})) }))
    }
    fn calls(&self) -> Vec<String> {
        self.calls.lock().unwrap().clone()
    }
}

async fn harness_with(app: Router) -> Harness {
    log();
    environment();
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
        status["expires_in"].as_u64().unwrap() > 1700,
        "half an hour once used"
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

impl Harness {
    /// A dashboard on the LAN asks for a pairing code: (pair reply, its cookie).
    async fn dashboard_pair(&self) -> (Value, String) {
        let mut request = Request::builder()
            .method("POST")
            .uri("/api/auth/pair")
            .header("host", "127.0.0.1:7861")
            .body(Body::empty())
            .unwrap();
        request.extensions_mut().insert(ConnectInfo(LAN.parse::<SocketAddr>().unwrap()));
        let response = reachy_controller::app(self.state.clone()).oneshot(request).await.unwrap();
        let cookie = response.headers()["set-cookie"].to_str().unwrap().split(';').next().unwrap().to_string();
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX).await.unwrap();
        (serde_json::from_slice(&bytes).unwrap(), cookie)
    }

    /// What `reachy-bridge setup` sees.
    async fn terminal(&self) -> Value {
        let key = self.key();
        let (status, body) = self.api("GET", "/api/setup/state", LOOPBACK, &[("x-reachy-bridge-key", &key)], None).await;
        assert_eq!(status, StatusCode::OK, "{body:?}");
        body
    }

    /// The terminal's answer to the waiting admin request: the code typed there.
    async fn confirm(&self, code: &str) -> (StatusCode, Value) {
        let key = self.key();
        self.api("POST", "/api/setup/confirm", LOOPBACK, &[("x-reachy-bridge-key", &key)], Some(json!({"code": code}))).await
    }
}

async fn propose(page: &str, cookie: &str, body: Value) -> (StatusCode, Value) {
    let r = client().post(format!("{page}/api/setup/admin")).header("cookie", cookie).json(&body).send().await.unwrap();
    let status = r.status();
    (status, r.json().await.unwrap_or(Value::Null))
}

async fn admin_status(page: &str, cookie: &str) -> Value {
    client().get(format!("{page}/api/setup/admin")).header("cookie", cookie).send().await.unwrap().json().await.unwrap()
}

#[tokio::test]
async fn the_terminal_confirms_the_dashboard_the_setup_browser_names_as_admin() {
    let h = harness().await;
    // A browser paired before the admin is logged out when the admin pairs.
    let (status, earlier) = h.api("POST", "/api/auth/pair", LAN, &[], None).await;
    assert_eq!(status, StatusCode::OK);
    let key = h.key();
    let (status, _) = h
        .api("POST", "/api/auth/approve", LOOPBACK, &[("x-reachy-bridge-key", &key)], Some(json!({"code": earlier["code"]})))
        .await;
    assert_eq!(status, StatusCode::OK);

    let (pair, pair_cookie) = h.dashboard_pair().await;
    let (code, page) = h.start().await;
    // The terminal's routes need the local key.
    for (method, path) in [("GET", "/api/setup/state"), ("POST", "/api/setup/confirm")] {
        let (status, _) = h.api(method, path, LAN, &[], Some(json!({"code": pair["code"]}))).await;
        assert_eq!(status, StatusCode::FORBIDDEN, "{path}");
    }
    let cookie = session(&login(&page, &code).await);
    let r = client().post(format!("{page}/api/setup/admin")).json(&json!({"code": pair["code"]})).send().await.unwrap();
    assert_eq!(r.status(), StatusCode::UNAUTHORIZED, "needs the setup session");
    let (status, _) = propose(&page, &cookie, json!({"code": wrong(pair["code"].as_str().unwrap()), "person": "Andry"})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "no dashboard shows that code");
    assert_eq!(h.terminal().await.get("proposal"), None);

    let (status, body) = propose(&page, &cookie, json!({"code": pair["code"], "person": "Andry", "name": "laptop"})).await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
    assert_eq!(body["status"], "waiting");
    assert_eq!(admin_status(&page, &cookie).await["status"], "waiting");
    // Nothing is approved until the terminal says so.
    assert!(!h.state.auth.admin_mode());
    let (status, _) = propose(&page, &cookie, json!({"code": pair["code"]})).await;
    assert_eq!(status, StatusCode::CONFLICT, "one request at a time");
    let terminal = h.terminal().await;
    assert_eq!(terminal["proposal"]["person"], "Andry");
    assert_eq!(terminal["proposal"]["peer"], "192.168.1.50", "the dashboard's address");
    assert_eq!(terminal["proposal"]["from"], "127.0.0.1", "the setup browser's address");

    let pair_code = pair["code"].as_str().unwrap();
    let (status, body) = h.confirm(pair_code).await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
    assert_eq!(body["role"], "admin");
    assert_eq!(admin_status(&page, &cookie).await["status"], "approved");
    assert_eq!(h.terminal().await.get("proposal"), None);
    let (status, _) = h.confirm(pair_code).await;
    assert_eq!(status, StatusCode::CONFLICT, "nothing waits");
    // One admin per page.
    let (another, _) = h.dashboard_pair().await;
    let (status, _) = propose(&page, &cookie, json!({"code": another["code"]})).await;
    assert_eq!(status, StatusCode::CONFLICT);
    // A new code from the terminal doesn't allow a second one.
    let (code2, _) = h.start().await;
    let cookie2 = session(&login(&page, &code2).await);
    let (status, _) = propose(&page, &cookie2, json!({"code": another["code"]})).await;
    assert_eq!(status, StatusCode::CONFLICT, "one admin per page");

    let id = pair["id"].as_str().unwrap();
    let (status, body) = h.api("GET", &format!("/api/auth/pair/{id}"), LAN, &[("cookie", &pair_cookie)], None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["status"], "approved");
    assert!(h.state.auth.admin_mode());
    let devices = std::fs::read_to_string(h.dir.join("devices.json")).unwrap();
    assert!(devices.contains("\"role\":\"admin\"") || devices.contains("\"role\": \"admin\""), "{devices}");
    assert!(devices.contains("Andry"));
    assert_eq!(devices.matches("token_sha256").count(), 1, "the earlier device is logged out");
    let logged = String::from_utf8_lossy(&log().lock().unwrap()).to_string();
    assert!(!logged.contains(pair["code"].as_str().unwrap()), "pairing code in the log");
}

#[tokio::test]
async fn the_terminal_refuses_a_code_its_dashboard_does_not_show() {
    let h = harness().await;
    let (mine, _) = h.dashboard_pair().await;
    let (theirs, _) = h.dashboard_pair().await;
    let (code, page) = h.start().await;
    let cookie = session(&login(&page, &code).await);
    // Someone proposes their own dashboard, with a name meant to mislead.
    let (status, _) = propose(&page, &cookie, json!({"code": theirs["code"], "person": "Andry\u{202e}\u{200b} (yes, this one)\n"})).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(h.terminal().await["proposal"]["person"], "Andry yes this one", "only plain text reaches the terminal");
    // The person at the robot types the code their own dashboard shows.
    let (status, body) = h.confirm(mine["code"].as_str().unwrap()).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["status"], "refused");
    assert!(!h.state.auth.admin_mode());
    assert_eq!(admin_status(&page, &cookie).await["status"], "refused");
    // Enter alone refuses too; then the right proposal goes through.
    let (status, _) = propose(&page, &cookie, json!({"code": mine["code"], "person": "Andry"})).await;
    assert_eq!(status, StatusCode::OK);
    let (_, body) = h.confirm("").await;
    assert_eq!(body["status"], "refused");
    let (status, _) = propose(&page, &cookie, json!({"code": mine["code"], "person": "Andry"})).await;
    assert_eq!(status, StatusCode::OK);
    let (status, body) = h.confirm(mine["code"].as_str().unwrap()).await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
    assert_eq!(body["role"], "admin");
}

#[tokio::test]
async fn refused_requests_end_the_session_too() {
    let h = harness().await;
    let (pair, _) = h.dashboard_pair().await;
    let (code, page) = h.start().await;
    let cookie = session(&login(&page, &code).await);
    for _ in 0..5 {
        let (status, _) = propose(&page, &cookie, json!({"code": pair["code"]})).await;
        assert_eq!(status, StatusCode::OK);
        let (_, body) = h.confirm("").await;
        assert_eq!(body["status"], "refused");
    }
    let r = client().get(format!("{page}/api/setup/admin")).header("cookie", &cookie).send().await.unwrap();
    assert_eq!(r.status(), StatusCode::UNAUTHORIZED, "the session is over");
}

#[tokio::test]
async fn codes_that_match_no_dashboard_end_the_session() {
    let h = harness().await;
    let (code, page) = h.start().await;
    let cookie = session(&login(&page, &code).await);
    for _ in 0..4 {
        let (status, _) = propose(&page, &cookie, json!({"code": "123456"})).await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
    }
    let (status, _) = propose(&page, &cookie, json!({"code": "123456"})).await;
    assert_eq!(status, StatusCode::LOCKED);
    let r = client().get(format!("{page}/api/setup/admin")).header("cookie", &cookie).send().await.unwrap();
    assert_eq!(r.status(), StatusCode::UNAUTHORIZED, "the session is over");
}

#[tokio::test]
async fn the_page_answers_only_to_the_robots_own_names() {
    let h = harness().await;
    let (_, page) = h.start().await;
    let port = page.rsplit(':').next().unwrap();
    for host in ["evil.example", "rebind.example:80", "a.evil.local", ".local"] {
        let r = client().get(format!("{page}/api/setup/status")).header("host", host).send().await.unwrap();
        assert_eq!(r.status(), StatusCode::MISDIRECTED_REQUEST, "{host}");
    }
    for host in [format!("127.0.0.1:{port}"), format!("localhost:{port}"), format!("[::1]:{port}"), "192.168.1.9".to_string(), format!("ubuntu-2.local:{port}")] {
        let r = client().get(format!("{page}/api/setup/status")).header("host", &host).send().await.unwrap();
        assert_eq!(r.status(), StatusCode::OK, "{host}");
    }
    // Cross-site reads of the setup API are refused too.
    let r = client().get(format!("{page}/api/setup/status")).header("sec-fetch-site", "cross-site").send().await.unwrap();
    assert_eq!(r.status(), StatusCode::FORBIDDEN);
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
    assert!(gone(&page).await, "the listener is gone");
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
    assert!(gone(&page).await);
}

impl Harness {
    /// The terminal's answer to the robot test.
    async fn run_robot_test(&self, approve: bool) -> (StatusCode, Value) {
        let key = self.key();
        self.api("POST", "/api/setup/robot-test/run", LOOPBACK, &[("x-reachy-bridge-key", &key)], Some(json!({"approve": approve}))).await
    }
}

async fn ask_robot_test(page: &str, cookie: &str) -> StatusCode {
    client().post(format!("{page}/api/setup/robot-test")).header("cookie", cookie).send().await.unwrap().status()
}

#[tokio::test]
async fn the_robot_test_runs_only_after_a_yes_on_the_terminal_and_ends_off() {
    let robot = FakeRobot::default();
    let h = harness_with(robot.router()).await;
    let (code, page) = h.start().await;
    let cookie = session(&login(&page, &code).await);
    let (status, _) = h.run_robot_test(true).await;
    assert_eq!(status, StatusCode::CONFLICT, "the page hasn't asked");
    let (status, _) = h.api("POST", "/api/setup/robot-test/run", LAN, &[], Some(json!({"approve": true}))).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "the terminal's route");
    assert_eq!(ask_robot_test(&page, &cookie).await, StatusCode::OK);
    assert_eq!(ask_robot_test(&page, &cookie).await, StatusCode::CONFLICT, "already waiting");
    assert_eq!(h.terminal().await["robot_test"], true);
    assert!(robot.calls().is_empty(), "nothing runs before the terminal answers");

    let (status, body) = h.run_robot_test(true).await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
    assert_eq!(body["status"], "passed", "{body:?}");
    assert!(body["detail"].as_str().unwrap().contains("torque off"), "{body:?}");
    // No wake-up move on, no sleep move off: the motors stay limp throughout.
    assert_eq!(robot.calls(), ["start?wake_up=false", "stop?goto_sleep=false"]);
    assert!(!*robot.on.lock().unwrap());
    let r: Value = client().get(format!("{page}/api/setup/robot-test")).header("cookie", &cookie).send().await.unwrap().json().await.unwrap();
    assert_eq!(r["status"], "passed");
    assert_eq!(h.terminal().await["robot_test"], false);
}

#[tokio::test]
async fn the_robot_test_never_touches_a_robot_that_is_on() {
    let robot = FakeRobot::default();
    *robot.on.lock().unwrap() = true;
    let h = harness_with(robot.router()).await;
    let (code, page) = h.start().await;
    let cookie = session(&login(&page, &code).await);
    assert_eq!(ask_robot_test(&page, &cookie).await, StatusCode::OK);
    let (_, body) = h.run_robot_test(true).await;
    assert_eq!(body["status"], "failed");
    assert!(body["detail"].as_str().unwrap().starts_with("Not run"), "{body:?}");
    assert!(robot.calls().is_empty(), "{:?}", robot.calls());
    assert!(*robot.on.lock().unwrap(), "left on");
    // A refusal on the terminal runs nothing either; the page can ask again.
    *robot.on.lock().unwrap() = false;
    assert_eq!(ask_robot_test(&page, &cookie).await, StatusCode::OK);
    let (_, body) = h.run_robot_test(false).await;
    assert_eq!(body["status"], "failed");
    assert!(robot.calls().is_empty());
    assert_eq!(ask_robot_test(&page, &cookie).await, StatusCode::OK);
}

#[tokio::test]
async fn the_robot_test_never_plays_the_sleep_move() {
    // Torque came up on: the test fails and stops the robot where it is,
    // with neither the sleep move nor a torque release (a held head would drop).
    let robot = FakeRobot::default();
    *robot.mode.lock().unwrap() = "enabled";
    let h = harness_with(robot.router()).await;
    let (code, page) = h.start().await;
    let cookie = session(&login(&page, &code).await);
    assert_eq!(ask_robot_test(&page, &cookie).await, StatusCode::OK);
    let (_, body) = h.run_robot_test(true).await;
    assert_eq!(body["status"], "failed", "{body:?}");
    assert!(body["detail"].as_str().unwrap().contains("mode enabled"), "{body:?}");
    assert_eq!(robot.calls(), ["start?wake_up=false", "stop?goto_sleep=false"]);
    assert!(!*robot.on.lock().unwrap());
}

#[tokio::test]
async fn a_busy_daemon_is_left_alone() {
    let robot = FakeRobot::default();
    *robot.refuse_start.lock().unwrap() = Some(409);
    let h = harness_with(robot.router()).await;
    let (code, page) = h.start().await;
    let cookie = session(&login(&page, &code).await);
    assert_eq!(ask_robot_test(&page, &cookie).await, StatusCode::OK);
    let (_, body) = h.run_robot_test(true).await;
    assert_eq!(body["status"], "failed");
    assert!(body["detail"].as_str().unwrap().contains("busy"), "{body:?}");
    assert_eq!(robot.calls(), ["start?wake_up=false"], "no stop for a start that isn't the test's");
}

#[tokio::test]
async fn the_device_check_reports_the_runtimes_json() {
    let h = harness().await;
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
