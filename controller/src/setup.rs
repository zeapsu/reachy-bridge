//! The setup page (#135): a temporary page on the LAN that walks a new
//! install through the device check and makes the user's dashboard the admin.
//!
//! `reachy-bridge setup` asks for it with the local key
//! (`POST /api/setup/start`, loopback only). The controller then listens on
//! every interface on `SETUP_PORT` (7870), LAN mode or not: that is the
//! point of the page. It answers with a one-time code, which the CLI prints
//! on the terminal. The code never goes through the log: it is in that HTTP
//! response and nowhere else.
//!
//! - The code is 6 digits from the OS random generator, single use and valid
//!   for [`CODE_TTL`]. [`MAX_WRONG`] wrong codes, from anyone, lock the page
//!   until the terminal asks for a new code (`start` again).
//! - The right code gives that browser the setup cookie ([`SETUP_COOKIE`]),
//!   tied to its address, good until the page closes. Only one browser gets it.
//! - The setup listener serves the setup routes and the dashboard bundle
//!   (which shows the setup screen there) and nothing else: no controller
//!   API, no app proxy, no daemon. It answers only under IP addresses, this
//!   machine's names and `.local` names, so a website's DNS-rebinding page
//!   can't reach it.
//! - The robot test (power on without the wake-up move, read the motors,
//!   power off) also runs only after a yes on the terminal: the page asks,
//!   the terminal answers. It runs only from off, under `power_lock`.
//! - It closes when setup finishes, when the terminal stops it, when the code
//!   expires unused, or [`SESSION_TTL`] after the code was used.
//!
//! **The admin is approved on the terminal.** The setup cookie crosses the
//! LAN over plain HTTP, so it can't be what grants admin. The browser only
//! *proposes* the pairing code its dashboard shows; the terminal then shows
//! who asked (the dashboard's address and the name typed) and the person at
//! the robot types the code their own dashboard shows. Only if it is the
//! proposed code is it approved, with the local key, exactly as
//! `reachy-bridge pair --admin` does: a proposal an attacker slipped in (a
//! stolen cookie, a spoofed packet) names the attacker's code, which the
//! person's dashboard doesn't show. One admin per page; after
//! [`MAX_PROPOSALS`] proposals that match no pairing request, the setup
//! session ends.

use std::collections::HashSet;
use std::net::{IpAddr, SocketAddr};
use std::path::PathBuf;
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use axum::extract::{ConnectInfo, Request, State};
use axum::http::{HeaderMap, HeaderValue, Method, header};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Extension, Json, Router};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::auth::{
    AuthContext, Role, cookie_header, cookies, cross_site, ct_eq, is_api, random_code, random_token, sha256,
};
use crate::error::{ApiError, ApiResult};
use crate::state::Shared;

pub const SETUP_COOKIE: &str = "rb_setup";
/// How long a code is valid before it is used.
pub const CODE_TTL: Duration = Duration::from_secs(15 * 60);
/// How long the page stays open once the code is used.
pub const SESSION_TTL: Duration = Duration::from_secs(30 * 60);
/// Wrong codes, from any browser, before the page locks.
pub const MAX_WRONG: u32 = 5;
/// Admin proposals matching no pairing request before the session ends.
pub const MAX_PROPOSALS: u32 = 5;
const DEFAULT_PORT: u16 = 7870;
/// The device check reads USB, GStreamer and the disk; give it time.
const CHECK_TIMEOUT: Duration = Duration::from_secs(300);

#[derive(Default)]
pub struct Setup {
    live: Mutex<Option<Live>>,
    /// A device check is running (one at a time).
    checking: AtomicBool,
}

/// What the setup browser asked for, and what the terminal answered.
#[derive(Clone)]
struct Proposal {
    code: String,
    person: String,
    name: String,
    /// The dashboard's address (the pairing request's peer).
    peer: IpAddr,
    outcome: Outcome,
}

#[derive(Clone, PartialEq)]
enum Outcome {
    Waiting,
    Approved,
    Refused(String),
}

/// The robot test, asked for by the page and answered on the terminal.
#[derive(Clone, PartialEq)]
enum RobotTest {
    None,
    Asked,
    Running,
    Done { passed: bool, detail: String },
}

impl RobotTest {
    fn json(&self) -> Value {
        match self {
            RobotTest::None => json!({"status": "none"}),
            RobotTest::Asked => json!({"status": "asked"}),
            RobotTest::Running => json!({"status": "running"}),
            RobotTest::Done { passed, detail } => {
                json!({"status": if *passed { "passed" } else { "failed" }, "detail": detail})
            }
        }
    }
}

struct Live {
    /// Empty once used.
    code: String,
    wrong: u32,
    session_sha256: Option<[u8; 32]>,
    /// The address that used the code; the session works from there only.
    session_peer: Option<IpAddr>,
    proposal: Option<Proposal>,
    missed_proposals: u32,
    /// An admin was approved from this page; a new code doesn't reset it.
    admin_made: bool,
    robot_test: RobotTest,
    /// When the page closes, unless setup finishes first.
    ends: Instant,
    port: u16,
    /// Host names the page answers to (besides IP literals).
    names: HashSet<String>,
    stop: tokio::sync::watch::Sender<bool>,
}

impl Live {
    /// A fresh code; it also ends any setup session.
    fn new_code(&mut self, now: Instant) -> String {
        self.code = random_code();
        self.wrong = 0;
        self.end_session();
        self.ends = now + CODE_TTL;
        self.code.clone()
    }

    fn end_session(&mut self) {
        self.session_sha256 = None;
        self.session_peer = None;
        self.proposal = None;
        self.missed_proposals = 0;
    }

    fn state(&self) -> Value {
        json!({
            "open": true,
            "port": self.port,
            "used": self.code.is_empty(),
            "in_use": self.session_sha256.is_some(),
            "locked": self.wrong >= MAX_WRONG,
            "expires_in": self.ends.saturating_duration_since(Instant::now()).as_secs(),
        })
    }
}

impl Setup {
    pub fn is_open(&self) -> bool {
        self.live.lock().unwrap().is_some()
    }

    /// Close the page, if open.
    pub fn close(&self, why: &str) {
        if let Some(live) = self.live.lock().unwrap().take() {
            let _ = live.stop.send(true);
            tracing::info!("setup: page on :{} closed ({why})", live.port);
        }
    }

    fn session(&self, headers: &HeaderMap, peer: Option<IpAddr>) -> bool {
        let live = self.live.lock().unwrap();
        let Some(live) = live.as_ref() else { return false };
        let Some(hash) = live.session_sha256 else { return false };
        peer.is_some()
            && peer == live.session_peer
            && cookies(headers, SETUP_COOKIE).take(2).any(|token| ct_eq(&sha256(token.as_bytes()), &hash))
    }
}

fn port() -> u16 {
    std::env::var("SETUP_PORT").ok().and_then(|p| p.parse().ok()).unwrap_or(DEFAULT_PORT)
}

/// This machine's host name, and `<name>.local`, lowercase.
fn host_names() -> HashSet<String> {
    let mut names = HashSet::from(["localhost".to_string()]);
    let name = std::fs::read_to_string("/proc/sys/kernel/hostname").ok().or_else(|| {
        std::process::Command::new("hostname").output().ok().and_then(|o| String::from_utf8(o.stdout).ok())
    });
    if let Some(name) = name.map(|n| n.trim().trim_end_matches('.').to_ascii_lowercase()).filter(|n| !n.is_empty()) {
        let short = name.split('.').next().unwrap_or(&name).to_string();
        names.extend([format!("{short}.local"), short, name]);
    }
    names
}

/// The Host header could name this machine: an IP literal, one of its names,
/// or a `.local` name.
fn own_host(headers: &HeaderMap, names: &HashSet<String>) -> bool {
    let Some(host) = headers.get(header::HOST).and_then(|h| h.to_str().ok()) else { return false };
    let name = match host.rsplit_once(':') {
        Some((name, port)) if !name.ends_with(':') && port.bytes().all(|b| b.is_ascii_digit()) => name,
        _ => host,
    };
    let name = name.trim_end_matches('.').to_ascii_lowercase();
    name.trim_matches(['[', ']']).parse::<IpAddr>().is_ok() || names.contains(&name) || mdns_name(&name)
}

/// `<one label>.local`: the name avahi publishes isn't always this machine's
/// host name (a conflict makes it ubuntu-2.local). Only the local network
/// answers for `.local`, so a website can't rebind one of these to the robot.
fn mdns_name(name: &str) -> bool {
    name.strip_suffix(".local").is_some_and(|label| !label.is_empty() && !label.contains('.'))
}

/// The terminal's side, for the local key only: open and close the page,
/// watch it, and answer the admin proposal.
pub fn router() -> Router<Shared> {
    Router::new()
        .route("/api/setup/start", post(start))
        .route("/api/setup/stop", post(stop))
        .route("/api/setup/state", get(terminal_state))
        .route("/api/setup/confirm", post(confirm))
        .route("/api/setup/robot-test/run", post(run_robot_test))
}

fn local_only(ctx: &AuthContext) -> ApiResult<()> {
    if ctx.local { Ok(()) } else { Err(ApiError::new(403, "only `reachy-bridge setup` on the robot can do this")) }
}

async fn start(State(state): State<Shared>, Extension(ctx): Extension<AuthContext>) -> ApiResult<Json<Value>> {
    local_only(&ctx)?;
    let now = Instant::now();
    {
        let mut live = state.setup.live.lock().unwrap();
        if let Some(live) = live.as_mut() {
            let code = live.new_code(now);
            tracing::info!("setup: new code issued for the page on :{}", live.port);
            return Ok(Json(json!({"code": code, "port": live.port, "expires_in": CODE_TTL.as_secs()})));
        }
    }
    // Two starts at once: the second bind fails and that start gets an error.
    let port = port();
    let listener = tokio::net::TcpListener::bind(("0.0.0.0", port))
        .await
        .map_err(|error| ApiError::new(500, format!("can't open the setup page on port {port}: {error}")))?;
    let port = listener.local_addr().map(|a| a.port()).unwrap_or(port);
    let (stop, mut stopped) = tokio::sync::watch::channel(false);
    let mut live = Live {
        code: String::new(),
        wrong: 0,
        session_sha256: None,
        session_peer: None,
        proposal: None,
        missed_proposals: 0,
        admin_made: false,
        robot_test: RobotTest::None,
        ends: now,
        port,
        names: host_names(),
        stop,
    };
    let code = live.new_code(now);
    *state.setup.live.lock().unwrap() = Some(live);
    let app = page_router(state.clone()).into_make_service_with_connect_info::<SocketAddr>();
    tokio::spawn(async move {
        let shutdown = async move {
            let _ = stopped.wait_for(|stop| *stop).await;
        };
        if let Err(error) = axum::serve(listener, app).with_graceful_shutdown(shutdown).await {
            tracing::error!(%error, "setup: page server failed");
        }
    });
    tokio::spawn(close_when_due(state.clone()));
    tracing::info!("setup: page open on 0.0.0.0:{port} for {} minutes", CODE_TTL.as_secs() / 60);
    Ok(Json(json!({"code": code, "port": port, "expires_in": CODE_TTL.as_secs()})))
}

async fn stop(State(state): State<Shared>, Extension(ctx): Extension<AuthContext>) -> ApiResult<Json<Value>> {
    local_only(&ctx)?;
    state.setup.close("stopped from the terminal");
    Ok(Json(json!({"status": "closed"})))
}

/// What the terminal shows: the page's state and any admin proposal waiting
/// for an answer.
async fn terminal_state(State(state): State<Shared>, Extension(ctx): Extension<AuthContext>) -> ApiResult<Json<Value>> {
    local_only(&ctx)?;
    let live = state.setup.live.lock().unwrap();
    let Some(live) = live.as_ref() else { return Ok(Json(json!({"open": false}))) };
    let mut body = live.state();
    if let Some(p) = live.proposal.as_ref().filter(|p| p.outcome == Outcome::Waiting) {
        body["proposal"] = json!({"person": p.person, "name": p.name, "peer": p.peer.to_string(), "from": live.session_peer.map(|a| a.to_string())});
    }
    body["robot_test"] = json!(live.robot_test == RobotTest::Asked);
    body["from"] = json!(live.session_peer.map(|a| a.to_string()));
    Ok(Json(body))
}

#[derive(Deserialize)]
struct ConfirmBody {
    /// The code the person's dashboard shows; empty or absent refuses.
    code: Option<String>,
}

/// The terminal's answer to the waiting proposal: the code typed there must
/// be the proposed one, still open from the address shown. Then it is
/// approved with the local key, as `reachy-bridge pair --admin` does.
async fn confirm(
    State(state): State<Shared>,
    Extension(ctx): Extension<AuthContext>,
    body: Result<Json<ConfirmBody>, axum::extract::rejection::JsonRejection>,
) -> ApiResult<Json<Value>> {
    local_only(&ctx)?;
    let Json(body) = body.map_err(|_| ApiError::bad_body())?;
    let mut slot = state.setup.live.lock().unwrap();
    let live = slot.as_mut().ok_or_else(|| ApiError::new(410, "setup is closed"))?;
    let proposal = live
        .proposal
        .as_mut()
        .filter(|p| p.outcome == Outcome::Waiting)
        .ok_or_else(|| ApiError::new(409, "no admin request is waiting"))?;
    let typed: String = body.code.unwrap_or_default().chars().filter(|c| !c.is_whitespace()).collect();
    let refusal = if typed.is_empty() {
        Some("refused on the robot's terminal")
    } else if !ct_eq(typed.as_bytes(), proposal.code.as_bytes()) {
        Some("the code typed on the robot's terminal is not the one this page sent")
    } else if state.auth.pending_peer(&typed) != Some(proposal.peer) {
        Some("that dashboard's code expired; get a new one on the dashboard")
    } else {
        None
    };
    if let Some(refusal) = refusal {
        proposal.outcome = Outcome::Refused(refusal.to_string());
        tracing::warn!("setup: admin request for the browser at {} refused: {refusal}", proposal.peer);
        // Refusals count too, so a hijacked session can't keep the terminal
        // asking about its own dashboard.
        live.missed_proposals += 1;
        if live.missed_proposals >= MAX_PROPOSALS {
            live.end_session();
        }
        return Ok(Json(json!({"status": "refused", "detail": refusal})));
    }
    let result = state.auth.approve_code(
        &proposal.code,
        crate::auth::ApproveAs {
            name: Some(&proposal.name),
            person: Some(&proposal.person),
            role: Role::Admin,
            shared: false,
        },
        "the setup page (confirmed on the terminal)",
        true,
    );
    match result {
        Ok(reply) => {
            proposal.outcome = Outcome::Approved;
            live.admin_made = true;
            tracing::info!("setup: the browser at {} is the admin (confirmed on the terminal)", proposal.peer);
            Ok(Json(reply))
        }
        Err(error) => {
            proposal.outcome = Outcome::Refused(error.detail.clone());
            Err(error)
        }
    }
}

/// Close the page when its time is up (the deadline moves when the code is
/// used or a new one is issued).
async fn close_when_due(state: Shared) {
    loop {
        tokio::time::sleep(Duration::from_secs(1)).await;
        let mut live = state.setup.live.lock().unwrap();
        let Some(current) = live.as_ref() else { return };
        if Instant::now() >= current.ends {
            let used = current.session_sha256.is_some();
            let current = live.take().expect("checked");
            let _ = current.stop.send(true);
            tracing::info!(
                "setup: page on :{} closed ({})",
                current.port,
                if used { "time is up" } else { "the code expired" }
            );
            return;
        }
    }
}

/// What the setup listener serves: the setup routes and the dashboard
/// bundle. Any other API path is 404.
fn page_router(state: Shared) -> Router {
    Router::new()
        .route("/api/setup/status", get(status))
        .route("/api/setup/login", post(login))
        .route("/api/setup/check", get(check))
        .route("/api/setup/admin", get(admin_status).post(propose_admin))
        .route("/api/setup/robot-test", get(robot_test_status).post(ask_robot_test))
        .route("/api/setup/finish", post(finish))
        .fallback(page_fallback)
        .layer(middleware::from_fn_with_state(state.clone(), page_guard))
        .with_state(state)
}

async fn page_fallback(method: Method, uri: axum::http::Uri) -> Response {
    if is_api(uri.path()) || method != Method::GET {
        return ApiError::new(404, "not found").into_response();
    }
    crate::routes::assets::serve(uri).await
}

fn peer_of(request: &Request) -> Option<IpAddr> {
    request.extensions().get::<ConnectInfo<SocketAddr>>().map(|c| c.0.ip().to_canonical())
}

/// Only the robot's own names; no cross-site calls; the setup routes past
/// status and login need the setup cookie from the address that used the
/// code; nothing frames the page.
async fn page_guard(State(state): State<Shared>, request: Request, next: Next) -> Response {
    let peer = peer_of(&request);
    let headers = request.headers();
    let path = request.uri().path().to_string();
    {
        let live = state.setup.live.lock().unwrap();
        let Some(live) = live.as_ref() else { return ApiError::new(410, "setup is closed").into_response() };
        if !own_host(headers, &live.names) {
            tracing::warn!("setup: refused {path} for another host name from {peer:?}");
            return ApiError::new(421, "wrong host").into_response();
        }
    }
    // Only the setup routes exist here, with or without the session.
    if is_api(&path) && !path.starts_with("/api/setup/") {
        return ApiError::new(404, "not found").into_response();
    }
    let api = is_api(&path);
    if (api || !request.method().is_safe() || headers.contains_key(header::UPGRADE)) && cross_site(headers, false) {
        tracing::warn!("setup: refused cross-site {path} from {peer:?}");
        return ApiError::new(403, "cross-site request refused").into_response();
    }
    let open = path == "/api/setup/status" || path == "/api/setup/login" || !api;
    if !open && !state.setup.session(headers, peer) {
        return ApiError::new(401, "enter the setup code first").into_response();
    }
    let mut response = next.run(request).await;
    let headers = response.headers_mut();
    headers.insert(header::X_FRAME_OPTIONS, HeaderValue::from_static("DENY"));
    headers.insert(header::CONTENT_SECURITY_POLICY, HeaderValue::from_static("frame-ancestors 'none'"));
    headers.insert(header::REFERRER_POLICY, HeaderValue::from_static("no-referrer"));
    if api {
        headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    }
    response
}

async fn status(
    State(state): State<Shared>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
) -> Json<Value> {
    let authenticated = state.setup.session(&headers, Some(peer.ip().to_canonical()));
    let live = state.setup.live.lock().unwrap();
    let mut body = live.as_ref().map_or_else(|| json!({"open": false}), Live::state);
    body["setup"] = json!(true);
    body["authenticated"] = json!(authenticated);
    if let Some(object) = body.as_object_mut() {
        object.remove("in_use");
        object.remove("port");
    }
    Json(body)
}

#[derive(Deserialize)]
struct LoginBody {
    code: String,
}

async fn login(
    State(state): State<Shared>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    body: Result<Json<LoginBody>, axum::extract::rejection::JsonRejection>,
) -> ApiResult<Response> {
    let Json(body) = body.map_err(|_| ApiError::bad_body())?;
    let peer = peer.ip().to_canonical();
    let code: String = body.code.chars().filter(|c| !c.is_whitespace()).collect();
    let now = Instant::now();
    let mut slot = state.setup.live.lock().unwrap();
    let live = slot.as_mut().filter(|l| now < l.ends).ok_or_else(|| ApiError::new(410, "setup is closed"))?;
    let new_code = "run `reachy-bridge setup` on the robot (or press Enter there) for a new one";
    if live.wrong >= MAX_WRONG {
        return Err(ApiError::new(423, format!("too many wrong codes: {new_code}")));
    }
    if live.code.is_empty() {
        return Err(ApiError::new(409, format!("this code was already used: {new_code}")));
    }
    if !ct_eq(code.as_bytes(), live.code.as_bytes()) {
        live.wrong += 1;
        let left = MAX_WRONG - live.wrong;
        tracing::warn!("setup: wrong code from {peer} ({} of {MAX_WRONG})", live.wrong);
        if left == 0 {
            return Err(ApiError::new(423, format!("too many wrong codes: {new_code}")));
        }
        return Err(ApiError::new(400, format!("wrong code; {left} tries left")));
    }
    let token = random_token::<32>();
    live.code.clear();
    live.session_sha256 = Some(sha256(token.as_bytes()));
    live.session_peer = Some(peer);
    live.ends = now + SESSION_TTL;
    tracing::info!("setup: code used by {peer}; the page stays open for {} minutes", SESSION_TTL.as_secs() / 60);
    let cookie = cookie_header(SETUP_COOKIE, &token, "/", SESSION_TTL.as_secs(), false);
    Ok(([(header::SET_COOKIE, cookie)], Json(json!({"status": "ok"}))).into_response())
}

/// The runtime's device check (`--doctor --json`): what `reachy-bridge
/// check` shows in the terminal. One at a time.
async fn check(State(state): State<Shared>) -> ApiResult<Json<Value>> {
    if state.setup.checking.swap(true, Ordering::SeqCst) {
        return Err(ApiError::new(409, "the device check is already running"));
    }
    struct Done<'a>(&'a AtomicBool);
    impl Drop for Done<'_> {
        fn drop(&mut self) {
            self.0.store(false, Ordering::SeqCst);
        }
    }
    let _done = Done(&state.setup.checking);
    let runtime =
        std::env::var_os("REACHY_BRIDGE_RUNTIME").filter(|p| !p.is_empty()).map(PathBuf::from).unwrap_or_else(|| {
            PathBuf::from(std::env::var_os("HOME").unwrap_or_default())
                .join(".local/share/reachy-bridge/.venv/bin/reachy-bridge-runtime")
        });
    let run = tokio::process::Command::new(&runtime).args(["--doctor", "--json"]).kill_on_drop(true).output();
    let output = tokio::time::timeout(CHECK_TIMEOUT, run)
        .await
        .map_err(|_| ApiError::new(504, "the device check took too long"))?
        .map_err(|error| ApiError::new(500, format!("can't run the device check ({}): {error}", runtime.display())))?;
    // It exits 1 when something failed; the report is on stdout either way.
    let results: Value = serde_json::from_slice(&output.stdout).map_err(|_| {
        ApiError::new(500, "the device check didn't produce a report; run `reachy-bridge check` on the robot")
    })?;
    Ok(Json(json!({"results": results})))
}

#[derive(Deserialize)]
struct AdminBody {
    code: String,
    person: Option<String>,
    name: Option<String>,
}

/// Ask the terminal to make the dashboard showing `code` the admin.
async fn propose_admin(
    State(state): State<Shared>,
    body: Result<Json<AdminBody>, axum::extract::rejection::JsonRejection>,
) -> ApiResult<Json<Value>> {
    let Json(body) = body.map_err(|_| ApiError::bad_body())?;
    let code: String = body.code.chars().filter(|c| !c.is_whitespace()).collect();
    if code.len() != 6 || !code.bytes().all(|b| b.is_ascii_digit()) {
        return Err(ApiError::new(400, "the code is 6 digits"));
    }
    // Printed on the robot's terminal: letters, digits, spaces and a little
    // punctuation only (no bidi overrides or zero-width tricks).
    let clean = |text: Option<&str>| -> String {
        let text: String = text
            .unwrap_or("")
            .chars()
            .map(|c| if c.is_whitespace() { ' ' } else { c })
            .filter(|c| c.is_alphanumeric() || " .-_'@".contains(*c))
            .take(64)
            .collect();
        text.split_whitespace().collect::<Vec<_>>().join(" ")
    };
    let (person, name) = (clean(body.person.as_deref()), clean(body.name.as_deref()));
    let peer = state.auth.pending_peer(&code);
    let mut slot = state.setup.live.lock().unwrap();
    let live = slot.as_mut().ok_or_else(|| ApiError::new(410, "setup is closed"))?;
    if live.admin_made {
        return Err(ApiError::new(409, "an admin was already made from this page"));
    }
    if live.proposal.as_ref().is_some_and(|p| p.outcome == Outcome::Waiting) {
        return Err(ApiError::new(409, "waiting for the robot's terminal to answer"));
    }
    let Some(peer) = peer else {
        live.missed_proposals += 1;
        tracing::warn!(
            "setup: admin request with a code that matches no pairing request ({} of {MAX_PROPOSALS})",
            live.missed_proposals
        );
        if live.missed_proposals >= MAX_PROPOSALS {
            live.end_session();
            return Err(ApiError::new(423, "too many codes that match no dashboard: enter a new setup code"));
        }
        return Err(ApiError::new(
            400,
            "no dashboard is showing that code; check it, or get a new one on the dashboard",
        ));
    };
    live.proposal = Some(Proposal { code, person: person.clone(), name, peer, outcome: Outcome::Waiting });
    tracing::info!("setup: admin request for the browser at {peer} waits for the terminal");
    Ok(Json(json!({"status": "waiting", "peer": peer.to_string(), "person": person})))
}

/// Where the admin request stands: none, waiting, approved or refused.
async fn admin_status(State(state): State<Shared>) -> Json<Value> {
    let live = state.setup.live.lock().unwrap();
    let proposal = live.as_ref().and_then(|l| l.proposal.clone());
    Json(match proposal.map(|p| p.outcome) {
        None => json!({"status": "none"}),
        Some(Outcome::Waiting) => json!({"status": "waiting"}),
        Some(Outcome::Approved) => json!({"status": "approved"}),
        Some(Outcome::Refused(detail)) => json!({"status": "refused", "detail": detail}),
    })
}

/// Ask the terminal to run the robot test.
async fn ask_robot_test(State(state): State<Shared>) -> ApiResult<Json<Value>> {
    let mut slot = state.setup.live.lock().unwrap();
    let live = slot.as_mut().ok_or_else(|| ApiError::new(410, "setup is closed"))?;
    if matches!(live.robot_test, RobotTest::Asked | RobotTest::Running) {
        return Err(ApiError::new(409, "the robot test is already waiting or running"));
    }
    live.robot_test = RobotTest::Asked;
    tracing::info!("setup: the page asks for the robot test");
    Ok(Json(live.robot_test.json()))
}

async fn robot_test_status(State(state): State<Shared>) -> Json<Value> {
    let live = state.setup.live.lock().unwrap();
    Json(live.as_ref().map_or_else(|| RobotTest::None.json(), |l| l.robot_test.json()))
}

#[derive(Deserialize)]
struct RunBody {
    approve: bool,
}

/// The terminal's answer to the robot test; a yes runs it and answers when
/// it is done.
async fn run_robot_test(
    State(state): State<Shared>,
    Extension(ctx): Extension<AuthContext>,
    body: Result<Json<RunBody>, axum::extract::rejection::JsonRejection>,
) -> ApiResult<Json<Value>> {
    local_only(&ctx)?;
    let Json(body) = body.map_err(|_| ApiError::bad_body())?;
    {
        let mut slot = state.setup.live.lock().unwrap();
        let live = slot.as_mut().ok_or_else(|| ApiError::new(410, "setup is closed"))?;
        if live.robot_test != RobotTest::Asked {
            return Err(ApiError::new(409, "the page hasn't asked for the robot test"));
        }
        if !body.approve {
            live.robot_test =
                RobotTest::Done { passed: false, detail: "Not run: refused on the robot's terminal.".to_string() };
            let reply = live.robot_test.json();
            // Like refused admin requests: a hijacked page can't keep asking.
            live.missed_proposals += 1;
            if live.missed_proposals >= MAX_PROPOSALS {
                live.end_session();
            }
            return Ok(Json(reply));
        }
        live.robot_test = RobotTest::Running;
    }
    // Its own task: a terminal that goes away mid-test (Ctrl-C, a timeout)
    // drops this request, and the test must still end off and report.
    let task = tokio::spawn(async move {
        let (passed, detail) = match robot_test(&state).await {
            Ok(detail) => (true, detail),
            Err(detail) => (false, detail),
        };
        tracing::info!("setup: robot test {}: {detail}", if passed { "passed" } else { "failed" });
        let result = RobotTest::Done { passed, detail };
        if let Some(live) = state.setup.live.lock().unwrap().as_mut() {
            live.robot_test = result.clone();
        }
        result
    });
    let result = task.await.map_err(|_| {
        ApiError::new(500, "the robot test stopped unexpectedly; check the dashboard that the robot is off")
    })?;
    Ok(Json(result.json()))
}

/// How long the backend gets to come up for the robot test.
const TEST_START_TIMEOUT: Duration = Duration::from_secs(15);

/// Power on without the wake-up move (the motors stay limp: no motion), read
/// the motors, power off where it is (no sleep move). Only from off, so it never
/// switches off a robot someone else switched on; it ends off.
async fn robot_test(state: &Shared) -> Result<String, String> {
    use crate::daemon::{DaemonError, backend_up, motor_mode};
    use crate::routes::power::POWER_OFF_TIMEOUT;
    let daemon = &state.daemon;
    let _power = state.power_lock.lock().await;
    let status =
        daemon.status().await.ok_or("The daemon isn't answering; check `reachy-bridge status` on the robot.")?;
    let robot = crate::daemon::robot_state(&status);
    // A daemon that never powered the robot on reports not_initialized: off too.
    if !matches!(robot.as_str(), "stopped" | "not_initialized") || backend_up(&status) {
        return Err(format!(
            "Not run: the robot is {robot}. The test only runs from off; switch it off from the dashboard and ask again."
        ));
    }
    // Off where it is: daemon/stop without the sleep move, which would move
    // the robot. Torque is left as it is, since releasing it could let a held
    // head drop. The dashboard's power-off (power_off_locked) plays the sleep
    // move unless the motors are already off, so it isn't used here.
    let off = || async {
        state.player.set_needs_home(true);
        // A stop sent while the daemon is still starting is refused as busy;
        // keep at it until the backend is down.
        let deadline = tokio::time::Instant::now() + POWER_OFF_TIMEOUT;
        loop {
            if tokio::time::Instant::now() >= deadline {
                return false;
            }
            if let Some(status) = daemon.status().await {
                let phase = status.get("state").and_then(Value::as_str);
                if !backend_up(&status) && phase != Some("starting") {
                    return true;
                }
                if backend_up(&status) && matches!(phase, Some("running" | "error")) {
                    let _ = daemon.post("daemon/stop", &[("goto_sleep", "false")]).await;
                }
            }
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
    };
    let still_on = " It may still be on: switch it off from the dashboard.";
    match daemon.post("daemon/start", &[("wake_up", "false")]).await {
        Ok(_) => {}
        // Busy: someone else is starting or stopping it. Not ours to switch off.
        Err(DaemonError::Refused { status: 409, .. }) => {
            return Err(
                "Not run: the daemon is busy starting or stopping the robot. Ask again in a moment.".to_string()
            );
        }
        Err(error) => {
            let up = daemon.status().await.is_some_and(|s| backend_up(&s));
            let ended_off = !up || off().await;
            return Err(format!(
                "The robot didn't power on: {}.{}",
                ApiError::from(error).detail,
                if ended_off { "" } else { still_on }
            ));
        }
    }
    let deadline = tokio::time::Instant::now() + TEST_START_TIMEOUT;
    let (up, last) = loop {
        let status = daemon.status().await.unwrap_or(Value::Null);
        if status.get("state").and_then(Value::as_str) == Some("running") && backend_up(&status) {
            break (true, status);
        }
        if tokio::time::Instant::now() >= deadline || status.get("state").and_then(Value::as_str) == Some("error") {
            break (false, status);
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    };
    if !up {
        let error = last.get("error").and_then(Value::as_str).unwrap_or("it didn't come up in time").to_string();
        let ended_off = off().await;
        return Err(format!(
            "The robot didn't power on ({error}). Check that its power supply is plugged in and switched on, and the USB cable.{}",
            if ended_off { "" } else { still_on }
        ));
    }
    let mode = motor_mode(&last).unwrap_or("unknown").to_string();
    let pose = daemon
        .get_query("state/full", &[])
        .await
        .ok()
        .and_then(|s| s.get("head_pose").cloned())
        .filter(|p| !p.is_null());
    if !off().await {
        return Err(format!("The robot powered on but didn't report off in time.{still_on}"));
    }
    if pose.is_none() {
        return Err("The robot powered on and off, but its motors didn't report a position. Check the power supply."
            .to_string());
    }
    if mode != "disabled" {
        return Err(format!(
            "The robot powered on and off without moving, but its motors came up in mode {mode}, not with torque off, so torque may still be on. Switch it on and off from the dashboard to put it to sleep properly."
        ));
    }
    Ok("The robot powered on without moving, its motors answered with torque off, as expected, and it powered off."
        .to_string())
}

async fn finish(State(state): State<Shared>) -> Json<Value> {
    state.setup.close("setup finished");
    Json(json!({"status": "closed"}))
}
