//! Route guards and pass-throughs: traversal, volume and the platform report. Against the scripted fake daemon in `common`.

mod common;

use common::*;

#[tokio::test]
async fn wildcard_routes_refuse_traversal_to_other_daemon_paths() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
    })
    .await;
    for uri in [
        "/api/expressions/play/..%2F..%2F..%2Fdaemon/start%3Fwake_up=true",
        "/api/expressions/play/%2e%2e/%2e%2e/daemon/stop",
        "/api/expressions/play/pollen-robotics/x/..%2F..%2Fmotors%2Fset_mode%2Fdisabled",
    ] {
        let (status, _) = h.call("POST", uri, None).await;
        assert_eq!(status, StatusCode::NOT_FOUND, "{uri}");
    }
    let (status, _) = h.call("GET", "/api/move/recorded-move-datasets/list/..%2F..%2F..%2Fdaemon%2Fstatus", None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert!(h.calls().is_empty(), "{:?}", h.calls());
}

#[tokio::test]
async fn volume_is_validated() {
    let h = harness(|_| {}).await;
    let (status, _) = h.call("POST", "/api/volume/set", Some(json!({"volume": 101}))).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    let (status, _) = h.call("POST", "/api/volume/set", Some(json!({"volume": "50"}))).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn platform_passes_the_runtime_report_through() {
    let h = harness(|_| {}).await;
    let (status, body) = h.call("GET", "/api/system/platform", None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["name"], "jetson");
    assert_eq!(body["tier"], "tested");
}
