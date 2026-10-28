//! The log stream. Against the scripted fake daemon in `common`.

mod common;

use common::*;

#[tokio::test]
async fn log_stream_rejects_unknown_sources_and_starts_with_a_reset() {
    let h = harness(|_| {}).await;
    let (status, _) = h.call("GET", "/api/logs/stream?source=nope", None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    h.state.logs.publish("daemon", "hello");
    let response = reachy_controller::app(h.state.clone())
        .oneshot(Request::get("/api/logs/stream?source=daemon").body(Body::empty()).unwrap())
        .await
        .unwrap();
    let mut body = response.into_body().into_data_stream();
    use futures_util::StreamExt;
    let first = body.next().await.unwrap().unwrap();
    let text = String::from_utf8_lossy(&first);
    assert!(text.starts_with("event: log-reset\ndata: "), "{text}");
    assert!(text.contains(r#""lines":["hello"]"#), "{text}");
}
