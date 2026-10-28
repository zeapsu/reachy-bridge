//! The app catalog and Hugging Face login (#126). Against the scripted fake
//! daemon in `common`.

mod common;

use std::net::SocketAddr;

use axum::extract::ConnectInfo;
use common::*;

/// A request from a peer, the way the server's ConnectInfo would carry it.
fn from(peer: &str, method: &str, uri: &str, body: Option<Value>) -> Request<Body> {
    let mut request = request(method, uri, body);
    request.extensions_mut().insert(ConnectInfo(peer.parse::<SocketAddr>().unwrap()));
    request
}

#[tokio::test]
async fn catalog_lists_curated_or_all_and_marks_installed() {
    let h = harness(|_| {}).await;
    let (status, body) = h.call("GET", "/api/apps/catalog", None).await;
    assert_eq!(status, StatusCode::OK);
    let apps = body["apps"].as_array().unwrap();
    assert_eq!(apps.len(), 2);
    assert_eq!(apps[0]["id"], "pollen-robotics/reachy_mini_radio");
    assert_eq!(apps[0]["installed"], true);
    assert_eq!(apps[1]["installed"], false);
    // Cached: a second read doesn't list again.
    h.call("GET", "/api/apps/catalog", None).await;
    assert_eq!(h.calls().iter().filter(|c| *c == "list curated").count(), 1);

    let (_, body) = h.call("GET", "/api/apps/catalog?all=true", None).await;
    let ids: Vec<&str> = body["apps"].as_array().unwrap().iter().map(|a| a["id"].as_str().unwrap()).collect();
    assert_eq!(ids, ["someone/other_app", "me/secret_app", "someone/hello_space"]);
    assert_eq!(body["apps"][1]["private"], true);
    // Installed as hello_world (its entry point), from the Space hello_space.
    assert_eq!(body["apps"][2]["installed"], true);
}

#[tokio::test]
async fn install_sends_the_daemons_own_entry() {
    let h = harness(|_| {}).await;
    let (status, body) = h.call("POST", "/api/apps/install", Some(json!({"id": "someone/other_app"}))).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["job"], "job-1");
    let install = h.calls().into_iter().find(|c| c.starts_with("install ")).unwrap();
    let sent: Value = serde_json::from_str(install.trim_start_matches("install ")).unwrap();
    assert_eq!(sent, catalog_entry("someone/other_app", false));
}

#[tokio::test]
async fn install_refuses_unlisted_installed_and_malformed() {
    let h = harness(|_| {}).await;
    for (id, code) in [
        (json!("evil/not_listed"), 404),
        (json!("pollen-robotics/reachy_mini_radio"), 409),
        (json!("someone/hello_space"), 409),
        (json!("../../daemon/stop"), 422),
        (json!("radio"), 422),
        (json!(7), 400),
    ] {
        let (status, _) = h.call("POST", "/api/apps/install", Some(json!({"id": id}))).await;
        assert_eq!(status.as_u16(), code, "{id}");
    }
    assert!(!h.calls().iter().any(|c| c.starts_with("install ")));
}

#[tokio::test]
async fn private_install_takes_a_space_id() {
    let h = harness(|_| {}).await;
    let (status, body) = h.call("POST", "/api/apps/install-private", Some(json!({"id": "me/secret_app"}))).await;
    assert_eq!((status, body["job"].as_str()), (StatusCode::OK, Some("job-2")));
    assert!(h.calls().contains(&"install-private me/secret_app".to_string()));
    let (status, _) = h.call("POST", "/api/apps/install-private", Some(json!({"id": "me/secret app"}))).await;
    assert_eq!(status.as_u16(), 422);
    // Only a private Space the listing shows: not a public one, not an unlisted one.
    for id in ["someone/other_app", "evil/unlisted"] {
        let (status, _) = h.call("POST", "/api/apps/install-private", Some(json!({"id": id}))).await;
        assert_eq!(status.as_u16(), 404, "{id}");
    }
    assert_eq!(h.calls().iter().filter(|c| c.starts_with("install-private")).count(), 1);
}

#[tokio::test]
async fn job_logs_are_tailed_and_scrubbed() {
    let h = harness(|_| {}).await;
    let (status, body) = h.call("GET", "/api/apps/jobs/job-1", None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["status"], "done");
    assert_eq!(body["logs"], json!(["Downloading", "token hf_*** leaked", "Installed"]));
    assert_eq!(h.call("GET", "/api/apps/jobs/nope", None).await.0.as_u16(), 404);
    assert_eq!(h.call("GET", "/api/apps/jobs/..", None).await.0.as_u16(), 404);
}

#[tokio::test]
async fn remove_and_update_refuse_the_running_app_and_unknown_names() {
    let h = harness(|f| f.app = Some(json!({"info": {"name": "reachy_mini_radio"}, "state": "running"}))).await;
    assert_eq!(h.call("POST", "/api/apps/remove/reachy_mini_radio", None).await.0.as_u16(), 409);
    assert_eq!(h.call("POST", "/api/apps/update/reachy_mini_radio", None).await.0.as_u16(), 409);
    assert_eq!(h.call("POST", "/api/apps/remove/not_installed", None).await.0.as_u16(), 404);
    let (status, body) = h.call("POST", "/api/apps/remove/hello_world", None).await;
    assert_eq!((status, body["job"].as_str()), (StatusCode::OK, Some("job-3")));
    let (status, body) = h.call("POST", "/api/apps/update/hello_world", None).await;
    assert_eq!((status, body["job"].as_str()), (StatusCode::OK, Some("job-4")));
    let calls = h.calls();
    assert!(calls.contains(&"remove hello_world".to_string()) && calls.contains(&"update hello_world".to_string()));
    assert!(!calls.iter().any(|c| c.contains("reachy_mini_radio") && !c.starts_with("list")));
}

#[tokio::test]
async fn updates_lists_app_names() {
    let h = harness(|_| {}).await;
    let (_, body) = h.call("GET", "/api/apps/updates", None).await;
    assert_eq!(body, json!({"updates": ["hello_world"]}));
}

#[tokio::test]
async fn hf_token_is_write_only_and_needs_a_secure_origin() {
    let h = harness(|_| {}).await;
    let (_, body) = h.call("GET", "/api/hf/status", None).await;
    assert_eq!(body, json!({"logged_in": false, "username": null}));

    // Plain HTTP from the LAN: refused before anything reaches the daemon.
    let (status, _) = h
        .send(from("192.168.1.20:5000", "PUT", "/api/hf/token", Some(json!({"token": "hf_goodtoken123"}))))
        .await;
    assert_eq!(status.as_u16(), 403);
    assert!(!h.calls().iter().any(|c| c.starts_with("save-token")));

    // HTTPS claimed by a LAN peer isn't HTTPS (only the local proxy's word counts).
    let mut request = from("192.168.1.20:5000", "PUT", "/api/hf/token", Some(json!({"token": "hf_goodtoken123"})));
    request.headers_mut().insert("x-forwarded-proto", "https".parse().unwrap());
    assert_eq!(h.send(request).await.0.as_u16(), 403);
    assert!(!h.calls().iter().any(|c| c.starts_with("save-token")));

    // A rejected token: the daemon's detail (which echoes it here) isn't passed on.
    let (status, body) = h
        .send(from("127.0.0.1:5000", "PUT", "/api/hf/token", Some(json!({"token": "hf_wrongtoken99"}))))
        .await;
    assert_eq!(status.as_u16(), 400);
    assert!(!body.to_string().contains("hf_wrongtoken99"));

    let (status, body) = h
        .send(from("127.0.0.1:5000", "PUT", "/api/hf/token", Some(json!({"token": "hf_goodtoken123"}))))
        .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body, json!({"logged_in": true, "username": "andry"}));
    let (_, body) = h.call("GET", "/api/hf/status", None).await;
    assert_eq!(body, json!({"logged_in": true, "username": "andry"}));

    // Not a token shape: refused locally.
    let (status, _) = h.send(from("127.0.0.1:5000", "PUT", "/api/hf/token", Some(json!({"token": "a b"})))).await;
    assert_eq!(status.as_u16(), 422);

    let (status, body) = h.call("DELETE", "/api/hf/token", None).await;
    assert_eq!((status, body["logged_in"].as_bool()), (StatusCode::OK, Some(false)));
    assert!(h.calls().contains(&"delete-token".to_string()));
}
