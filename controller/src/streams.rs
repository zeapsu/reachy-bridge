//! Lazy shared upstreams: one daemon poll or WebSocket per stream, fanned out
//! to every dashboard tab.
//!
//! A stream's upstream task starts with its first subscriber and stops once
//! the last one leaves. New subscribers get the latest sample immediately
//! (a `watch` channel keeps only the newest value, so a slow tab skips samples
//! instead of queueing them).

use std::future::Future;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use futures_util::StreamExt;
use serde_json::{json, Value};
use tokio::sync::watch;

use crate::daemon::Daemon;
use crate::host;

/// One sample. `None` means "nothing to send" (no sample yet, or a stats
/// sample withheld while the daemon is offline).
pub type Sample = Option<Arc<Value>>;

pub struct LazyStream {
    tx: watch::Sender<Sample>,
    task: Mutex<Option<tokio::task::JoinHandle<()>>>,
    /// Drop the last sample when the last subscriber leaves, so a returning
    /// tab doesn't flash a stale value (audio levels).
    clear_when_idle: bool,
}

impl LazyStream {
    pub fn new(clear_when_idle: bool) -> Arc<Self> {
        Arc::new(Self { tx: watch::channel(None).0, task: Mutex::new(None), clear_when_idle })
    }

    /// Subscribe, starting the upstream with `run` if it isn't running.
    pub fn subscribe<F, Fut>(self: &Arc<Self>, run: F) -> watch::Receiver<Sample>
    where
        F: FnOnce(watch::Sender<Sample>) -> Fut,
        Fut: Future<Output = ()> + Send + 'static,
    {
        let mut task = self.task.lock().unwrap();
        let idle = task.as_ref().is_none_or(|t| t.is_finished());
        if idle && self.clear_when_idle && self.tx.receiver_count() == 0 {
            self.tx.send_replace(None);
        }
        // Subscribe before spawning: the task ends when no receiver is left.
        let mut rx = self.tx.subscribe();
        rx.mark_changed();
        if idle {
            let upstream = run(self.tx.clone());
            let tx = self.tx.clone();
            *task = Some(tokio::spawn(async move {
                // Stop as soon as the last subscriber is gone.
                tokio::select! {
                    () = upstream => {}
                    () = tx.closed() => {}
                }
            }));
        }
        rx
    }
}

/// The daemon's full-state WebSocket, reconnecting after a 1 s pause. A
/// dropped connection publishes `{"error": "daemon_unavailable"}`.
///
/// The daemon refuses the socket (503, logged as two error lines) until its
/// backend runs, so while the robot is off this polls `daemon/status` instead
/// and connects only once the daemon reports `running` (#160).
pub async fn run_state(daemon: Daemon, tx: watch::Sender<Sample>) {
    let url = daemon.ws_url("state/ws/full?with_doa=true");
    loop {
        let running = daemon
            .status()
            .await
            .is_some_and(|s| s.get("state").and_then(Value::as_str) == Some("running"));
        if running && let Ok((mut ws, _)) = tokio_tungstenite::connect_async(url.as_str()).await {
            while let Some(Ok(message)) = ws.next().await {
                if let Ok(text) = message.to_text()
                    && let Ok(value) = serde_json::from_str::<Value>(text)
                {
                    tx.send_replace(Some(Arc::new(value)));
                }
            }
        }
        tx.send_replace(Some(Arc::new(json!({"error": "daemon_unavailable"}))));
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
}

const AUDIO_FIELDS: [&str; 9] =
    ["rms", "rms_db", "low", "mid", "high", "spectrum", "sample_rate_hz", "source", "timestamp"];
const SPECTRUM_LENGTH: usize = 18;

/// Keep only a well-formed spectrum sample, reduced to the fields the
/// dashboard reads.
pub fn valid_audio_levels(value: &Value) -> Option<Value> {
    let unit = |v: &Value| v.as_f64().is_some_and(|x| x.is_finite() && (0.0..=1.0).contains(&x));
    let non_empty = |v: &Value| v.as_str().is_some_and(|s| !s.trim().is_empty());
    let object = value.as_object()?;
    if !AUDIO_FIELDS.iter().all(|f| object.contains_key(*f)) {
        return None;
    }
    let spectrum = object["spectrum"].as_array()?;
    let valid = ["rms", "low", "mid", "high"].iter().all(|f| unit(&object[*f]))
        && spectrum.len() == SPECTRUM_LENGTH
        && spectrum.iter().all(unit)
        && object["rms_db"].as_f64().is_some_and(|x| (-60.0..=0.0).contains(&x))
        && object["sample_rate_hz"].as_f64().is_some_and(|x| x.is_finite() && x > 0.0)
        && non_empty(&object["source"])
        && non_empty(&object["timestamp"]);
    valid.then(|| Value::Object(AUDIO_FIELDS.iter().map(|f| (f.to_string(), object[*f].clone())).collect()))
}

/// Poll the daemon's microphone spectrum at 10 Hz (1 s back-off on failure).
pub async fn run_audio(daemon: Daemon, tx: watch::Sender<Sample>) {
    loop {
        let levels = daemon.get("audio/levels").await.ok().and_then(|v| valid_audio_levels(&v));
        let delay = if levels.is_some() { Duration::from_millis(100) } else { Duration::from_secs(1) };
        let sample = levels.unwrap_or_else(|| json!({"error": "daemon_unavailable"}));
        tx.send_replace(Some(Arc::new(sample)));
        tokio::time::sleep(delay).await;
    }
}

/// Host stats every 1.5 s, withheld while the daemon is offline.
pub async fn run_stats(daemon: Daemon, tx: watch::Sender<Sample>) {
    loop {
        let sample = match daemon.status().await {
            None => None,
            Some(_) => tokio::task::spawn_blocking(host::collect_stats).await.ok().map(Arc::new),
        };
        tx.send_replace(sample);
        tokio::time::sleep(Duration::from_millis(1500)).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn levels() -> Value {
        json!({
            "rms": 0.2, "rms_db": -14.0, "low": 0.1, "mid": 0.2, "high": 0.3,
            "spectrum": vec![0.5; SPECTRUM_LENGTH], "sample_rate_hz": 16000,
            "source": "XVF3800", "timestamp": "2026-10-23T00:00:00Z", "extra": 1,
        })
    }

    #[test]
    fn audio_levels_keep_known_fields() {
        let kept = valid_audio_levels(&levels()).unwrap();
        assert!(kept.get("extra").is_none());
        assert_eq!(kept["source"], "XVF3800");
    }

    #[test]
    fn audio_levels_reject_out_of_range() {
        let mut bad = levels();
        bad["rms"] = json!(1.5);
        assert!(valid_audio_levels(&bad).is_none());
        let mut short = levels();
        short["spectrum"] = json!([0.1]);
        assert!(valid_audio_levels(&short).is_none());
        let mut quiet = levels();
        quiet["rms_db"] = json!(-61);
        assert!(valid_audio_levels(&quiet).is_none());
    }

    #[tokio::test]
    async fn upstream_stops_with_the_last_subscriber() {
        let stream = LazyStream::new(true);
        let rx = stream.subscribe(|tx| async move {
            tx.send_replace(Some(Arc::new(json!(1))));
            std::future::pending::<()>().await;
        });
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert_eq!(**rx.borrow().as_ref().unwrap(), json!(1));
        drop(rx);
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert!(stream.task.lock().unwrap().as_ref().unwrap().is_finished());
        // A new subscriber restarts it without the stale sample.
        let rx = stream.subscribe(|_tx| std::future::pending::<()>());
        assert!(rx.borrow().is_none());
    }
}
