//! The setup page (#135): a temporary page on the LAN that walks a new
//! install through the device check and pairs its first admin.
//!
//! `reachy-bridge setup` asks for it with the local key
//! (`POST /api/setup/start`, loopback only). The controller then listens on
//! every interface on `SETUP_PORT` (7870) and answers with a one-time code,
//! which the CLI prints on the terminal. The code never goes through the log:
//! it is in that HTTP response and nowhere else.
//!
//! - The code is 6 digits from the OS random generator, single use and valid
//!   for [`CODE_TTL`]. [`MAX_WRONG`] wrong codes, from anyone, lock the page
//!   until the terminal asks for a new code (`start` again).
//! - The right code gives that browser the setup cookie ([`SETUP_COOKIE`]),
//!   good until the page closes. Only one browser gets it.
//! - The setup listener serves the setup routes and the dashboard bundle
//!   (which shows the setup screen there) and nothing else: no controller
//!   API, no app proxy, no daemon.
//! - It closes when setup finishes, when the terminal stops it, when the code
//!   expires unused, or [`SESSION_TTL`] after the code was used.
//!
//! Holding the code proves access to the robot's terminal, the same as the
//! local key, so the setup browser may approve the first admin: it enters the
//! pairing code its dashboard shows, and that request is approved as an
//! admin, exactly as `reachy-bridge pair --admin` would.
//!
//! The code crosses the LAN over plain HTTP; that is why it is short-lived
//! and single-use.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Mutex;
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
    AuthContext, Role, cookie_header, cookies, cross_site, ct_eq, is_api, random_code,
    random_token, sha256,
};
use crate::error::{ApiError, ApiResult};
use crate::state::Shared;

pub const SETUP_COOKIE: &str = "rb_setup";
/// How long a code is valid before it is used.
pub const CODE_TTL: Duration = Duration::from_secs(15 * 60);
/// How long the page stays open once the code is used.
pub const SESSION_TTL: Duration = Duration::from_secs(60 * 60);
/// Wrong codes, from any browser, before the page locks.
pub const MAX_WRONG: u32 = 5;
const DEFAULT_PORT: u16 = 7870;
/// The device check reads USB, GStreamer and the disk; give it time.
const CHECK_TIMEOUT: Duration = Duration::from_secs(300);

#[derive(Default)]
pub struct Setup {
    live: Mutex<Option<Live>>,
}

struct Live {
    /// Empty once used.
    code: String,
    wrong: u32,
    session_sha256: Option<[u8; 32]>,
    /// When the page closes, unless setup finishes first.
    ends: Instant,
    port: u16,
    stop: tokio::sync::watch::Sender<bool>,
}

impl Live {
    fn new_code(&mut self, now: Instant) -> String {
        self.code = random_code();
        self.wrong = 0;
        self.session_sha256 = None;
        self.ends = now + CODE_TTL;
        self.code.clone()
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

    fn session(&self, headers: &HeaderMap) -> bool {
        let live = self.live.lock().unwrap();
        let Some(hash) = live.as_ref().and_then(|l| l.session_sha256) else {
            return false;
        };
        cookies(headers, SETUP_COOKIE)
            .take(2)
            .any(|token| ct_eq(&sha256(token.as_bytes()), &hash))
    }
}

fn port() -> u16 {
    std::env::var("SETUP_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(DEFAULT_PORT)
}

/// `/api/setup/start` and `/api/setup/stop` on the controller itself, for
/// the local key only.
pub fn router() -> Router<Shared> {
    Router::new()
        .route("/api/setup/start", post(start))
        .route("/api/setup/stop", post(stop))
}

fn local_only(ctx: &AuthContext) -> ApiResult<()> {
    if ctx.local {
        Ok(())
    } else {
        Err(ApiError::new(
            403,
            "only `reachy-bridge setup` on the robot can open the setup page",
        ))
    }
}

async fn start(
    State(state): State<Shared>,
    Extension(ctx): Extension<AuthContext>,
) -> ApiResult<Json<Value>> {
    local_only(&ctx)?;
    let now = Instant::now();
    {
        let mut live = state.setup.live.lock().unwrap();
        if let Some(live) = live.as_mut() {
            let code = live.new_code(now);
            tracing::info!("setup: new code issued for the page on :{}", live.port);
            return Ok(Json(
                json!({"code": code, "port": live.port, "expires_in": CODE_TTL.as_secs()}),
            ));
        }
    }
    let port = port();
    let listener = tokio::net::TcpListener::bind(("0.0.0.0", port))
        .await
        .map_err(|error| {
            ApiError::new(
                500,
                format!("can't open the setup page on port {port}: {error}"),
            )
        })?;
    let port = listener.local_addr().map(|a| a.port()).unwrap_or(port);
    let (stop, mut stopped) = tokio::sync::watch::channel(false);
    let code = {
        let mut slot = state.setup.live.lock().unwrap();
        if slot.is_some() {
            // Another start won the race; use its page.
            drop(listener);
            let live = slot.as_mut().expect("checked");
            return Ok(Json(
                json!({"code": live.new_code(now), "port": live.port, "expires_in": CODE_TTL.as_secs()}),
            ));
        }
        let mut live = Live {
            code: String::new(),
            wrong: 0,
            session_sha256: None,
            ends: now,
            port,
            stop,
        };
        let code = live.new_code(now);
        *slot = Some(live);
        code
    };
    let app = page_router(state.clone()).into_make_service_with_connect_info::<SocketAddr>();
    tokio::spawn(async move {
        let shutdown = async move {
            let _ = stopped.wait_for(|stop| *stop).await;
        };
        if let Err(error) = axum::serve(listener, app)
            .with_graceful_shutdown(shutdown)
            .await
        {
            tracing::error!(%error, "setup: page server failed");
        }
    });
    tokio::spawn(close_when_due(state.clone()));
    tracing::info!(
        "setup: page open on 0.0.0.0:{port} for {} minutes",
        CODE_TTL.as_secs() / 60
    );
    Ok(Json(
        json!({"code": code, "port": port, "expires_in": CODE_TTL.as_secs()}),
    ))
}

async fn stop(
    State(state): State<Shared>,
    Extension(ctx): Extension<AuthContext>,
) -> ApiResult<Json<Value>> {
    local_only(&ctx)?;
    state.setup.close("stopped from the terminal");
    Ok(Json(json!({"status": "closed"})))
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
                if used {
                    "time is up"
                } else {
                    "the code expired"
                }
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
        .route("/api/setup/admin", post(admin))
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

/// Cross-site requests are refused; the setup routes past status and login
/// need the setup cookie; nothing frames the page.
async fn page_guard(State(state): State<Shared>, request: Request, next: Next) -> Response {
    let peer = request
        .extensions()
        .get::<ConnectInfo<SocketAddr>>()
        .map(|c| c.0.ip().to_canonical());
    let headers = request.headers();
    let path = request.uri().path().to_string();
    if !state.setup.is_open() {
        return ApiError::new(410, "setup is closed").into_response();
    }
    if (!request.method().is_safe() || headers.contains_key(header::UPGRADE))
        && cross_site(headers, false)
    {
        tracing::warn!("setup: refused cross-site {path} from {peer:?}");
        return ApiError::new(403, "cross-site request refused").into_response();
    }
    // Only the setup routes exist here, with or without the session.
    if is_api(&path) && !path.starts_with("/api/setup/") {
        return ApiError::new(404, "not found").into_response();
    }
    let open = path == "/api/setup/status" || path == "/api/setup/login" || !is_api(&path);
    if !open && !state.setup.session(headers) {
        return ApiError::new(401, "enter the setup code first").into_response();
    }
    let mut response = next.run(request).await;
    let headers = response.headers_mut();
    headers.insert(header::X_FRAME_OPTIONS, HeaderValue::from_static("DENY"));
    headers.insert(
        header::CONTENT_SECURITY_POLICY,
        HeaderValue::from_static("frame-ancestors 'none'"),
    );
    headers.insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("no-referrer"),
    );
    if is_api(&path) {
        headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    }
    response
}

async fn status(State(state): State<Shared>, headers: HeaderMap) -> Json<Value> {
    let authenticated = state.setup.session(&headers);
    let live = state.setup.live.lock().unwrap();
    let (locked, used, expires_in) = live.as_ref().map_or((false, false, 0), |l| {
        (
            l.wrong >= MAX_WRONG,
            l.code.is_empty(),
            l.ends.saturating_duration_since(Instant::now()).as_secs(),
        )
    });
    Json(
        json!({"setup": true, "authenticated": authenticated, "locked": locked, "used": used, "expires_in": expires_in}),
    )
}

#[derive(Deserialize)]
struct LoginBody {
    code: String,
}

async fn login(
    State(state): State<Shared>,
    body: Result<Json<LoginBody>, axum::extract::rejection::JsonRejection>,
) -> ApiResult<Response> {
    let Json(body) = body.map_err(|_| ApiError::bad_body())?;
    let code: String = body.code.chars().filter(|c| !c.is_whitespace()).collect();
    let now = Instant::now();
    let mut slot = state.setup.live.lock().unwrap();
    let live = slot
        .as_mut()
        .ok_or_else(|| ApiError::new(410, "setup is closed"))?;
    if live.wrong >= MAX_WRONG {
        return Err(ApiError::new(
            423,
            "too many wrong codes: run `reachy-bridge setup` on the robot for a new one",
        ));
    }
    if live.code.is_empty() {
        return Err(ApiError::new(
            409,
            "this code was already used: run `reachy-bridge setup` on the robot for a new one",
        ));
    }
    if !ct_eq(code.as_bytes(), live.code.as_bytes()) {
        live.wrong += 1;
        let left = MAX_WRONG - live.wrong;
        tracing::warn!("setup: wrong code ({} of {MAX_WRONG})", live.wrong);
        if left == 0 {
            return Err(ApiError::new(
                423,
                "too many wrong codes: run `reachy-bridge setup` on the robot for a new one",
            ));
        }
        return Err(ApiError::new(400, format!("wrong code; {left} tries left")));
    }
    let token = random_token::<32>();
    live.code.clear();
    live.session_sha256 = Some(sha256(token.as_bytes()));
    live.ends = now + SESSION_TTL;
    tracing::info!(
        "setup: code used; the page stays open for {} minutes",
        SESSION_TTL.as_secs() / 60
    );
    let cookie = cookie_header(SETUP_COOKIE, &token, "/", SESSION_TTL.as_secs(), false);
    Ok((
        [(header::SET_COOKIE, cookie)],
        Json(json!({"status": "ok"})),
    )
        .into_response())
}

/// The runtime's device check (`--doctor --json`): what `reachy-bridge
/// check` shows in the terminal.
async fn check() -> ApiResult<Json<Value>> {
    let runtime = std::env::var_os("REACHY_BRIDGE_RUNTIME")
        .filter(|p| !p.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(std::env::var_os("HOME").unwrap_or_default())
                .join(".local/share/reachy-bridge/.venv/bin/reachy-bridge-runtime")
        });
    let run = tokio::process::Command::new(&runtime)
        .args(["--doctor", "--json"])
        .kill_on_drop(true)
        .output();
    let output = tokio::time::timeout(CHECK_TIMEOUT, run)
        .await
        .map_err(|_| ApiError::new(504, "the device check took too long"))?
        .map_err(|error| {
            ApiError::new(
                500,
                format!(
                    "can't run the device check ({}): {error}",
                    runtime.display()
                ),
            )
        })?;
    // It exits 1 when something failed; the report is on stdout either way.
    let results: Value = serde_json::from_slice(&output.stdout).map_err(|_| {
        ApiError::new(
            500,
            "the device check didn't produce a report; run `reachy-bridge check` on the robot",
        )
    })?;
    Ok(Json(json!({"results": results})))
}

#[derive(Deserialize)]
struct AdminBody {
    code: String,
    person: Option<String>,
    name: Option<String>,
}

/// Approve the dashboard's pairing code as the admin.
async fn admin(
    State(state): State<Shared>,
    body: Result<Json<AdminBody>, axum::extract::rejection::JsonRejection>,
) -> ApiResult<Json<Value>> {
    let Json(body) = body.map_err(|_| ApiError::bad_body())?;
    state
        .auth
        .approve_code(
            &body.code,
            body.name.as_deref(),
            body.person.as_deref(),
            Role::Admin,
            "the setup page",
            true,
        )
        .map(Json)
}

async fn finish(State(state): State<Shared>) -> Json<Value> {
    state.setup.close("setup finished");
    Json(json!({"status": "closed"}))
}
