//! The app UI proxy (#158) against a fake daemon and fake app servers on
//! loopback ports.

use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::body::{Body, Bytes};
use axum::extract::ws::{Message, WebSocketUpgrade};
use axum::http::{HeaderMap, Method, StatusCode, Uri};
use axum::response::IntoResponse;
use axum::routing::get;
use axum::{Json, Router};
use futures_util::{SinkExt, StreamExt};
use reachy_controller::app_proxy::PORT_TTL;
use reachy_controller::daemon::Daemon;
use reachy_controller::logs::LogBroker;
use reachy_controller::state::AppState;
use serde_json::{json, Value};
use tokio_tungstenite::tungstenite;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;

async fn serve(router: Router) -> u16 {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        axum::serve(listener, router.into_make_service_with_connect_info::<SocketAddr>()).await.unwrap()
    });
    port
}

/// A fake app UI: `/` names it, `/echo` reflects the request, `/stream`
/// sends one chunk and then holds the response open, `/ws` echoes.
async fn fake_app(label: &'static str) -> u16 {
    async fn echo(method: Method, uri: Uri, headers: HeaderMap, body: Bytes) -> Json<Value> {
        let header = |name: &str| headers.get(name).and_then(|v| v.to_str().ok()).map(str::to_string);
        Json(json!({
            "method": method.as_str(),
            "uri": uri.to_string(),
            "host": header("host"),
            "proto": header("x-forwarded-proto"),
            "forwarded_host": header("x-forwarded-host"),
            "forwarded_for": header("x-forwarded-for"),
            "cookie": header("cookie"),
            "private": header("x-private"),
            "body": String::from_utf8_lossy(&body),
        }))
    }
    async fn stream() -> impl IntoResponse {
        let chunks = futures_util::stream::once(async { Ok::<_, std::io::Error>(Bytes::from("one\n")) })
            .chain(futures_util::stream::pending());
        ([("content-type", "text/event-stream")], Body::from_stream(chunks))
    }
    async fn ws(upgrade: WebSocketUpgrade) -> impl IntoResponse {
        upgrade.protocols(["echo.v1"]).on_upgrade(|mut socket| async move {
            while let Some(Ok(message)) = socket.recv().await {
                let reply = match message {
                    Message::Text(text) => Message::Text(format!("echo {}", text.as_str()).into()),
                    Message::Binary(data) => Message::Binary(data),
                    Message::Close(_) => break,
                    _ => continue,
                };
                if socket.send(reply).await.is_err() {
                    break;
                }
            }
        })
    }
    serve(
        Router::new()
            .route("/", get(move || async move { label }))
            .route("/echo", get(echo).post(echo))
            .route("/stream", get(stream))
            .route("/redirect", get(|| async { (StatusCode::FOUND, [("location", "/elsewhere")]) }))
            .route("/ws", get(ws)),
    )
    .await
}

type Current = Arc<Mutex<Value>>;

/// A fake daemon with four installed apps (A and B answer, C's port is
/// closed, D claims one of the controller's own ports) and a settable
/// current app.
async fn fake_daemon(ports: [u16; 4], current: Current) -> String {
    let installed = json!([
        {"name": "app_a", "extra": {"custom_app_url": format!("http://0.0.0.0:{}", ports[0])}},
        {"name": "app_b", "extra": {"custom_app_url": format!("http://0.0.0.0:{}/", ports[1])}},
        {"name": "app_c", "extra": {"custom_app_url": format!("http://0.0.0.0:{}", ports[2])}},
        {"name": "app_d", "extra": {"custom_app_url": format!("http://0.0.0.0:{}", ports[3])}},
        {"name": "no_ui", "extra": {}},
    ]);
    let router = Router::new()
        .route("/api/apps/list-available/installed", get(move || async move { Json(installed) }))
        .route("/api/apps/current-app-status", get(move || async move { Json(current.lock().unwrap().clone()) }));
    format!("http://127.0.0.1:{}", serve(router).await)
}

fn running(name: &str) -> Value {
    json!({"info": {"name": name}, "state": "running"})
}

struct Harness {
    current: Current,
    proxy: String,
    proxy_port: u16,
}

impl Harness {
    async fn new(app: Value) -> Self {
        let closed = || std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        let own = fake_app("controller").await;
        let ports = [fake_app("app A").await, fake_app("app B").await, closed(), own];
        let current = Arc::new(Mutex::new(app));
        let daemon = fake_daemon(ports, current.clone()).await;
        let dir = std::env::temp_dir().join(format!("rc-proxy-test-{}-{}", std::process::id(), ports[0]));
        let state = AppState::new(Daemon::new(&daemon, Duration::from_secs(2)), LogBroker::new(), dir, Duration::from_millis(10));
        let proxy_port = serve(reachy_controller::app_proxy::router(state, &[own])).await;
        Self { current, proxy: format!("http://127.0.0.1:{proxy_port}"), proxy_port }
    }

    async fn switch(&self, app: Value) {
        *self.current.lock().unwrap() = app;
        tokio::time::sleep(PORT_TTL + Duration::from_millis(100)).await;
    }

    async fn get(&self, path: &str) -> (StatusCode, String) {
        let response = reqwest::get(format!("{}{path}", self.proxy)).await.unwrap();
        (response.status(), response.text().await.unwrap())
    }
}

#[tokio::test]
async fn forwards_method_path_query_headers_and_body() {
    let h = Harness::new(running("app_a")).await;
    let client = reqwest::Client::new();
    let response = client
        .post(format!("{}/echo?x=1&y=two", h.proxy))
        .header("host", "robot.example:8043")
        .header("x-forwarded-proto", "https")
        .header("cookie", "session=1")
        .header("connection", "x-private")
        .header("x-private", "hop")
        .body("hello")
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let echoed: Value = response.json().await.unwrap();
    assert_eq!(echoed["method"], "POST");
    assert_eq!(echoed["uri"], "/echo?x=1&y=two");
    assert_eq!(echoed["host"], "robot.example:8043");
    assert_eq!(echoed["proto"], "https");
    assert_eq!(echoed["forwarded_host"], "robot.example:8043");
    assert_eq!(echoed["forwarded_for"], "127.0.0.1");
    assert_eq!(echoed["cookie"], "session=1");
    assert_eq!(echoed["private"], Value::Null);
    assert_eq!(echoed["body"], "hello");

    let echoed: Value = client.get(format!("{}/echo", h.proxy)).send().await.unwrap().json().await.unwrap();
    assert_eq!(echoed["method"], "GET");
    assert_eq!(echoed["proto"], "http");
}

#[tokio::test]
async fn redirects_are_passed_through_not_followed() {
    let h = Harness::new(running("app_a")).await;
    let client = reqwest::Client::builder().redirect(reqwest::redirect::Policy::none()).build().unwrap();
    let response = client.get(format!("{}/redirect", h.proxy)).send().await.unwrap();
    assert_eq!(response.status(), StatusCode::FOUND);
    assert_eq!(response.headers()["location"], "/elsewhere");
}

#[tokio::test]
async fn responses_stream_without_buffering() {
    let h = Harness::new(running("app_a")).await;
    let mut response = reqwest::get(format!("{}/stream", h.proxy)).await.unwrap();
    assert_eq!(response.headers()["content-type"], "text/event-stream");
    // The app never ends this response: the first chunk must arrive anyway.
    let chunk = tokio::time::timeout(Duration::from_secs(2), response.chunk()).await.expect("first chunk");
    assert_eq!(chunk.unwrap().unwrap(), "one\n");
}

#[tokio::test]
async fn websocket_echo_through_the_proxy() {
    let h = Harness::new(running("app_a")).await;
    let mut request = format!("ws://127.0.0.1:{}/ws", h.proxy_port).into_client_request().unwrap();
    request.headers_mut().insert("sec-websocket-protocol", "other, echo.v1".parse().unwrap());
    let (mut socket, response) = tokio_tungstenite::connect_async(request).await.unwrap();
    assert_eq!(response.headers()["sec-websocket-protocol"], "echo.v1");
    socket.send(tungstenite::Message::text("hi")).await.unwrap();
    let reply = tokio::time::timeout(Duration::from_secs(2), socket.next()).await.unwrap().unwrap().unwrap();
    assert_eq!(reply.to_text().unwrap(), "echo hi");
    socket.send(tungstenite::Message::Binary(Bytes::from_static(&[1, 2, 3]))).await.unwrap();
    let reply = tokio::time::timeout(Duration::from_secs(2), socket.next()).await.unwrap().unwrap().unwrap();
    assert_eq!(reply.into_data(), Bytes::from_static(&[1, 2, 3]));
    // A browser-initiated close completes its handshake through the proxy.
    socket.close(None).await.unwrap();
    let reply = tokio::time::timeout(Duration::from_secs(2), socket.next()).await.unwrap();
    assert!(matches!(reply, Some(Ok(tungstenite::Message::Close(_)))), "{reply:?}");
}

#[tokio::test]
async fn no_app_is_a_503_page_linking_to_the_dashboard() {
    let h = Harness::new(Value::Null).await;
    let response = reqwest::Client::new()
        .get(format!("{}/anything", h.proxy))
        .header("x-forwarded-host", "robot.example:8043")
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert!(response.headers()["content-type"].to_str().unwrap().starts_with("text/html"));
    let page = response.text().await.unwrap();
    assert!(page.contains("No app with a web UI is running."));
    assert!(page.contains(r#"href="//robot.example:10000/""#));

    // A stopped app, or one without a UI, is the same.
    h.switch(json!({"info": {"name": "app_a"}, "state": "stopped"})).await;
    assert_eq!(h.get("/").await.0, StatusCode::SERVICE_UNAVAILABLE);
    h.switch(running("no_ui")).await;
    assert_eq!(h.get("/").await.0, StatusCode::SERVICE_UNAVAILABLE);
}

#[tokio::test]
async fn an_app_whose_server_is_down_is_a_503() {
    let h = Harness::new(running("app_c")).await;
    let (status, page) = h.get("/").await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert!(page.contains("isn't answering yet"));
}

#[tokio::test]
async fn never_forwards_to_the_controllers_own_ports() {
    let h = Harness::new(running("app_d")).await;
    let (status, page) = h.get("/").await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert!(page.contains("No app with a web UI is running."));
}

#[tokio::test]
async fn follows_an_app_switch() {
    let h = Harness::new(running("app_a")).await;
    assert_eq!(h.get("/").await, (StatusCode::OK, "app A".into()));
    h.switch(running("app_b")).await;
    assert_eq!(h.get("/").await, (StatusCode::OK, "app B".into()));
    h.switch(Value::Null).await;
    assert_eq!(h.get("/").await.0, StatusCode::SERVICE_UNAVAILABLE);
    h.switch(json!({"info": {"name": "app_a"}, "state": "starting"})).await;
    assert_eq!(h.get("/").await, (StatusCode::OK, "app A".into()));
}
