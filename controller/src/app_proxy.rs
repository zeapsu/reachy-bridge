//! Reverse proxy for the running app's own web UI (#158).
//!
//! The dashboard is served over HTTPS by `tailscale serve`, but an app's UI
//! listens on plain http (`http://0.0.0.0:8042`, ...), where browsers block
//! the microphone and camera. The controller's second listener
//! (`127.0.0.1:$CONTROLLER_APP_PORT`, default 7863) forwards everything at
//! the root path to the current app's UI on loopback, and `tailscale serve`
//! puts HTTPS :8043 in front of it. The root path keeps apps' absolute paths
//! (`/static/...`, `/ws`) working; only one app runs at a time, so one port
//! is enough.
//!
//! The upstream port always comes from the daemon (its current app and that
//! app's `custom_app_url`), never from the request. It is cached for
//! [`PORT_TTL`] and dropped as soon as the upstream refuses a connection, so
//! an app switch is followed within a second.
//!
//! HTTP bodies stream both ways (SSE and downloads are never buffered).
//! WebSocket upgrades are accepted here and piped message by message to a
//! client connection to the app.

use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use axum::body::{Body, HttpBody};
use axum::extract::ws::{self, WebSocket, WebSocketUpgrade};
use axum::extract::{ConnectInfo, FromRequestParts, Request, State};
use axum::http::header::{self, HeaderMap, HeaderName, HeaderValue};
use axum::http::StatusCode;
use axum::response::{Html, IntoResponse, Response};
use axum::Router;
use futures_util::{SinkExt, StreamExt};
use serde_json::Value;
use tokio_tungstenite::tungstenite;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;

use crate::auth::{cross_site, strip_credential_cookies, strip_credentials};
use crate::state::Shared;

/// How long a resolved upstream port is reused before asking the daemon again.
pub const PORT_TTL: Duration = Duration::from_secs(1);

/// The dashboard's HTTPS port under `tailscale serve` (`reachy-bridge serve`).
/// The 503 page links to `//<host>:DASHBOARD_SERVE_PORT/`.
pub const DASHBOARD_SERVE_PORT: u16 = 10000;

/// Request and response headers that apply to one hop only (RFC 9110 §7.6.1).
const HOP_BY_HOP: [&str; 8] = [
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
];

/// Headers the WebSocket client handshake to the app sets itself.
const WS_HANDSHAKE: [&str; 4] = ["sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions", "sec-websocket-accept"];

pub struct AppProxy {
    state: Shared,
    http: reqwest::Client,
    cached: Mutex<Option<(Option<u16>, Instant)>>,
    /// The controller's own listeners: never an upstream, whatever an app's
    /// manifest says (that would loop, or re-expose the controller API).
    own_ports: Vec<u16>,
}

type Proxy = Arc<AppProxy>;

/// Why there is no upstream to forward to.
enum Unavailable {
    NoApp,
    Daemon,
    NotAnswering,
}

/// How long the app gets to answer a WebSocket upgrade.
const WS_CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

/// The proxy's router. `own_ports` are the controller's listening ports.
pub fn router(state: Shared, own_ports: &[u16]) -> Router {
    let http = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .connect_timeout(Duration::from_secs(2))
        .build()
        .expect("reqwest client");
    let proxy = Arc::new(AppProxy { state, http, cached: Mutex::new(None), own_ports: own_ports.to_vec() });
    Router::new().fallback(forward).with_state(proxy)
}

impl AppProxy {
    /// UI port of the daemon's current app, while it is starting or running.
    async fn port(&self) -> Result<u16, Unavailable> {
        if let Some((port, at)) = *self.cached.lock().unwrap()
            && at.elapsed() < PORT_TTL
        {
            return port.ok_or(Unavailable::NoApp);
        }
        let current = self.state.apps.current().await.map_err(|_| Unavailable::Daemon)?;
        let name = current
            .as_ref()
            .filter(|c| matches!(c.get("state").and_then(Value::as_str), Some("starting" | "running")))
            .and_then(|c| c.get("info")?.get("name")?.as_str().map(str::to_string));
        let port = match name {
            Some(name) => self
                .state
                .apps
                .ui_port(&name)
                .await
                .map_err(|_| Unavailable::Daemon)?
                .filter(|port| !self.own_ports.contains(port)),
            None => None,
        };
        *self.cached.lock().unwrap() = Some((port, Instant::now()));
        port.ok_or(Unavailable::NoApp)
    }

    fn forget(&self) {
        *self.cached.lock().unwrap() = None;
    }
}

async fn forward(State(proxy): State<Proxy>, request: Request) -> Response {
    let (mut parts, body) = request.into_parts();
    let host = forwarded_host(&parts.headers);
    let peer = parts.extensions.get::<ConnectInfo<SocketAddr>>().map(|c| c.0);
    if !proxy.state.auth.app_proxy_allowed(&parts.headers, peer.map(|p| p.ip().to_canonical())) {
        return login_required(host.as_deref());
    }
    // Another site can't make a tailnet browser act through the app (a plain
    // form POST); the app's own page is same-origin. tailscale serve keeps
    // Host, so X-Forwarded-Host isn't needed (and isn't believed).
    if (!parts.method.is_safe() || is_upgrade(&parts.headers)) && cross_site(&parts.headers, false) {
        tracing::warn!("app proxy: refused cross-site {} {}", parts.method, parts.uri.path());
        return (StatusCode::FORBIDDEN, "cross-site request refused").into_response();
    }
    let mut headers = parts.headers.clone();
    strip_hop_by_hop(&mut headers);
    strip_credentials(&mut headers);
    let port = match proxy.port().await {
        Ok(port) => port,
        Err(why) => return unavailable(why, host.as_deref()),
    };
    add_forwarded(&mut headers, &parts.headers, peer);
    let path = parts.uri.path_and_query().map_or("/", |pq| pq.as_str()).to_string();

    if is_upgrade(&parts.headers) {
        let upgrade = match WebSocketUpgrade::from_request_parts(&mut parts, &()).await {
            Ok(upgrade) => upgrade,
            Err(rejection) => return rejection.into_response(),
        };
        return websocket(proxy, upgrade, port, &path, headers, host.as_deref()).await;
    }

    let url = format!("http://127.0.0.1:{port}{path}");
    let mut upstream = proxy.http.request(parts.method, url).headers(headers);
    if body.size_hint().exact() != Some(0) {
        upstream = upstream.body(reqwest::Body::wrap_stream(body.into_data_stream()));
    }
    match upstream.send().await {
        Ok(response) => {
            let mut out = Response::builder().status(response.status());
            if let Some(out_headers) = out.headers_mut() {
                *out_headers = response.headers().clone();
                strip_hop_by_hop(out_headers);
                strip_credential_cookies(out_headers);
            }
            out.body(Body::from_stream(response.bytes_stream())).unwrap_or_else(|_| StatusCode::BAD_GATEWAY.into_response())
        }
        Err(error) => {
            if error.is_connect() {
                proxy.forget();
            }
            tracing::debug!(%error, "app UI request failed");
            unavailable(Unavailable::NotAnswering, host.as_deref())
        }
    }
}

/// Connect to the app first, so its chosen subprotocol can be answered, then
/// accept the browser's upgrade and pipe messages both ways.
async fn websocket(
    proxy: Proxy,
    upgrade: WebSocketUpgrade,
    port: u16,
    path: &str,
    mut headers: HeaderMap,
    host: Option<&str>,
) -> Response {
    let Ok(mut request) = format!("ws://127.0.0.1:{port}{path}").into_client_request() else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    for name in WS_HANDSHAKE {
        headers.remove(name);
    }
    // Keep the browser's Host (the app sees the same origin it was loaded
    // from); the client request has already set the rest of the handshake.
    for name in headers.keys() {
        request.headers_mut().remove(name);
    }
    for (name, value) in &headers {
        request.headers_mut().append(name.clone(), value.clone());
    }
    let connected = tokio::time::timeout(WS_CONNECT_TIMEOUT, tokio_tungstenite::connect_async(request)).await;
    let Ok(connected) = connected else {
        return unavailable(Unavailable::NotAnswering, host);
    };
    let (app, response) = match connected {
        Ok(connected) => connected,
        Err(tungstenite::Error::Http(response)) => {
            // The app refused the upgrade: pass its status on.
            return StatusCode::from_u16(response.status().as_u16())
                .unwrap_or(StatusCode::BAD_GATEWAY)
                .into_response();
        }
        Err(error) => {
            if matches!(error, tungstenite::Error::Io(_)) {
                proxy.forget();
            }
            tracing::debug!(%error, "app UI WebSocket failed");
            return unavailable(Unavailable::NotAnswering, host);
        }
    };
    let protocol = response
        .headers()
        .get(header::SEC_WEBSOCKET_PROTOCOL)
        .and_then(|p| p.to_str().ok())
        .map(str::to_string);
    let upgrade = match protocol {
        Some(protocol) => upgrade.protocols([protocol]),
        None => upgrade,
    };
    upgrade.on_upgrade(move |browser| pipe(browser, app))
}

type AppSocket = tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

/// Copy messages until either side closes. Pings and pongs stay per hop:
/// both WebSocket stacks answer pings themselves.
async fn pipe(browser: WebSocket, app: AppSocket) {
    let (mut browser_tx, mut browser_rx) = browser.split();
    let (mut app_tx, mut app_rx) = app.split();
    let up = async {
        while let Some(Ok(message)) = browser_rx.next().await {
            let message = match message {
                ws::Message::Text(text) => tungstenite::Message::text(text.as_str()),
                ws::Message::Binary(data) => tungstenite::Message::Binary(data),
                ws::Message::Close(frame) => {
                    let frame = frame.map(|f| tungstenite::protocol::CloseFrame {
                        code: f.code.into(),
                        reason: f.reason.as_str().into(),
                    });
                    let _ = app_tx.send(tungstenite::Message::Close(frame)).await;
                    break;
                }
                ws::Message::Ping(_) | ws::Message::Pong(_) => continue,
            };
            if app_tx.send(message).await.is_err() {
                break;
            }
        }
        let _ = app_tx.close().await;
    };
    let down = async {
        while let Some(Ok(message)) = app_rx.next().await {
            let message = match message {
                tungstenite::Message::Text(text) => ws::Message::Text(text.as_str().into()),
                tungstenite::Message::Binary(data) => ws::Message::Binary(data),
                tungstenite::Message::Close(frame) => {
                    let frame =
                        frame.map(|f| ws::CloseFrame { code: f.code.into(), reason: f.reason.as_str().into() });
                    let _ = browser_tx.send(ws::Message::Close(frame)).await;
                    break;
                }
                tungstenite::Message::Ping(_) | tungstenite::Message::Pong(_) | tungstenite::Message::Frame(_) => {
                    continue;
                }
            };
            if browser_tx.send(message).await.is_err() {
                break;
            }
        }
        let _ = browser_tx.close().await;
    };
    // Once one side closes, give the other a moment to finish the closing
    // handshake (so both see a clean close), then drop both connections.
    tokio::pin!(up, down);
    tokio::select! {
        () = &mut up => { let _ = tokio::time::timeout(Duration::from_secs(1), &mut down).await; }
        () = &mut down => { let _ = tokio::time::timeout(Duration::from_secs(1), &mut up).await; }
    }
}

fn is_upgrade(headers: &HeaderMap) -> bool {
    headers
        .get(header::UPGRADE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.eq_ignore_ascii_case("websocket"))
}

/// Drop hop-by-hop headers, including any the Connection header names.
fn strip_hop_by_hop(headers: &mut HeaderMap) {
    let named: Vec<HeaderName> = headers
        .get_all(header::CONNECTION)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|v| v.split(','))
        .filter_map(|name| HeaderName::from_bytes(name.trim().as_bytes()).ok())
        .collect();
    for name in named {
        headers.remove(name);
    }
    for name in HOP_BY_HOP {
        headers.remove(name);
    }
}

/// Keep what `tailscale serve` already set (this listener is loopback-only,
/// so only local processes reach it); otherwise describe this hop.
fn add_forwarded(headers: &mut HeaderMap, incoming: &HeaderMap, peer: Option<SocketAddr>) {
    if !incoming.contains_key("x-forwarded-proto") {
        headers.insert("x-forwarded-proto", HeaderValue::from_static("http"));
    }
    if !incoming.contains_key("x-forwarded-host")
        && let Some(host) = incoming.get(header::HOST)
    {
        headers.insert("x-forwarded-host", host.clone());
    }
    if let Some(peer) = peer {
        let value = match incoming.get("x-forwarded-for").and_then(|v| v.to_str().ok()) {
            Some(existing) => format!("{existing}, {}", peer.ip()),
            None => peer.ip().to_string(),
        };
        if let Ok(value) = HeaderValue::from_str(&value) {
            headers.insert("x-forwarded-for", value);
        }
    }
}

/// The host name the browser used, without its port, if it is a plain name
/// or address (it ends up in the 503 page's link).
fn forwarded_host(headers: &HeaderMap) -> Option<String> {
    let host = headers
        .get("x-forwarded-host")
        .or_else(|| headers.get(header::HOST))?
        .to_str()
        .ok()?
        .trim();
    let name = if host.starts_with('[') {
        &host[..=host.find(']')?]
    } else {
        host.rsplit_once(':').map_or(host, |(name, _)| name)
    };
    let valid = !name.is_empty()
        && name.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '[' | ']' | ':'));
    valid.then(|| name.to_string())
}

/// A small page instead of a bare error: the dashboard is the way back.
/// LAN mode with `CONTROLLER_TRUST_TAILNET=0` and no paired device.
fn login_required(host: Option<&str>) -> Response {
    let link = host.map_or_else(String::new, |host| {
        format!(r#"<p><a href="//{host}:{DASHBOARD_SERVE_PORT}/">Pair this device on the dashboard</a></p>"#)
    });
    let page = format!(
        "<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\">\
         <title>Login required</title></head><body style=\"font-family:sans-serif;margin:2rem\">\
         <p>This device isn't paired with the robot.</p>{link}</body></html>"
    );
    let mut response = (StatusCode::UNAUTHORIZED, Html(page)).into_response();
    response.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    response
}

fn unavailable(why: Unavailable, host: Option<&str>) -> Response {
    let message = match why {
        Unavailable::NoApp => "No app with a web UI is running.",
        Unavailable::Daemon => "The robot daemon isn't answering.",
        Unavailable::NotAnswering => "The app's web UI isn't answering yet. Reload in a few seconds.",
    };
    let link = host.map_or_else(String::new, |host| {
        format!(r#"<p><a href="//{host}:{DASHBOARD_SERVE_PORT}/">Back to the dashboard</a></p>"#)
    });
    let page = format!(
        "<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\">\
         <title>App unavailable</title></head><body style=\"font-family:sans-serif;margin:2rem\">\
         <p>{message}</p>{link}</body></html>"
    );
    let mut response = (StatusCode::SERVICE_UNAVAILABLE, Html(page)).into_response();
    response.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    response.headers_mut().insert(header::RETRY_AFTER, HeaderValue::from_static("2"));
    response
}

#[cfg(test)]
mod tests {
    use super::*;

    fn with_host(host: &str) -> HeaderMap {
        let mut headers = HeaderMap::new();
        headers.insert(header::HOST, HeaderValue::from_str(host).unwrap());
        headers
    }

    #[test]
    fn host_drops_the_port_and_rejects_markup() {
        assert_eq!(forwarded_host(&with_host("robot.example:8043")).as_deref(), Some("robot.example"));
        assert_eq!(forwarded_host(&with_host("robot")).as_deref(), Some("robot"));
        assert_eq!(forwarded_host(&with_host("[::1]:7863")).as_deref(), Some("[::1]"));
        assert_eq!(forwarded_host(&with_host("a\"b")), None);
        let mut headers = with_host("127.0.0.1:7863");
        headers.insert("x-forwarded-host", HeaderValue::from_static("robot.example"));
        assert_eq!(forwarded_host(&headers).as_deref(), Some("robot.example"));
    }

    #[test]
    fn hop_by_hop_headers_and_connection_named_ones_are_dropped() {
        let mut headers = HeaderMap::new();
        headers.insert(header::CONNECTION, HeaderValue::from_static("keep-alive, x-private"));
        headers.insert("x-private", HeaderValue::from_static("1"));
        headers.insert(header::TRANSFER_ENCODING, HeaderValue::from_static("chunked"));
        headers.insert(header::COOKIE, HeaderValue::from_static("a=1"));
        strip_hop_by_hop(&mut headers);
        assert_eq!(headers.len(), 1);
        assert!(headers.contains_key(header::COOKIE));
    }
}
