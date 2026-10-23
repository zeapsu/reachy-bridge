//! Log sources for the system panel: the controller's own log and the daemon
//! service's journal (app output goes there too).
//!
//! Each source keeps a bounded ring of recent lines and a status, and fans
//! new lines out over a broadcast channel. Subscribing takes the ring
//! snapshot and the receiver under one lock, so no line falls between them.

use std::collections::{HashMap, VecDeque};
use std::io::Write;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::{Arc, Mutex};

use tokio::io::{AsyncBufRead, AsyncBufReadExt, BufReader};
use tokio::sync::broadcast;
use tracing::field::{Field, Visit};
use tracing_subscriber::layer::Context;
use tracing_subscriber::Layer;

pub const SOURCES: [&str; 2] = ["controller", "daemon"];
pub const RING_SIZE: usize = 500;
pub const REPLAY_LINES: usize = 200;
const CHANNEL_SIZE: usize = 256;
pub const MAX_LINE_BYTES: usize = 64 * 1024;

#[derive(Clone, Debug, PartialEq)]
pub enum LogEvent {
    Line(String),
    Status(String),
}

struct Source {
    ring: VecDeque<String>,
    status: String,
    tx: broadcast::Sender<LogEvent>,
}

pub struct Snapshot {
    pub lines: Vec<String>,
    pub status: String,
    pub rx: broadcast::Receiver<LogEvent>,
}

#[derive(Default)]
pub struct LogBroker {
    sources: Mutex<HashMap<String, Source>>,
}

impl LogBroker {
    pub fn new() -> Arc<Self> {
        let broker = Arc::new(Self::default());
        broker.set_status("controller", "live");
        broker.set_status("daemon", "empty");
        broker
    }

    fn with_source<T>(&self, source: &str, f: impl FnOnce(&mut Source) -> T) -> T {
        let mut sources = self.sources.lock().unwrap();
        let entry = sources.entry(source.to_string()).or_insert_with(|| Source {
            ring: VecDeque::with_capacity(RING_SIZE),
            status: "empty".into(),
            tx: broadcast::channel(CHANNEL_SIZE).0,
        });
        f(entry)
    }

    pub fn status(&self, source: &str) -> String {
        self.with_source(source, |s| s.status.clone())
    }

    pub fn set_status(&self, source: &str, status: &str) {
        self.with_source(source, |s| {
            if s.status != status {
                s.status = status.to_string();
                let _ = s.tx.send(LogEvent::Status(status.to_string()));
            }
        });
    }

    /// Append a line; a source that wasn't live becomes live.
    pub fn publish(&self, source: &str, line: &str) {
        let line = line.trim_end_matches(['\r', '\n']).to_string();
        self.with_source(source, |s| {
            if s.status != "live" {
                s.status = "live".into();
                let _ = s.tx.send(LogEvent::Status("live".into()));
            }
            if s.ring.len() == RING_SIZE {
                s.ring.pop_front();
            }
            s.ring.push_back(line.clone());
            let _ = s.tx.send(LogEvent::Line(line));
        });
    }

    pub fn subscribe(&self, source: &str) -> Snapshot {
        self.with_source(source, |s| {
            let skip = s.ring.len().saturating_sub(REPLAY_LINES);
            Snapshot { lines: s.ring.iter().skip(skip).cloned().collect(), status: s.status.clone(), rx: s.tx.subscribe() }
        })
    }
}

/// Publish lines from `reader` to `source`. A line over [`MAX_LINE_BYTES`] is
/// dropped whole, with one notice, and following carries on.
pub async fn pump_lines<R: AsyncBufRead + Unpin>(mut reader: R, broker: &LogBroker, source: &str, prefix: &str) {
    let mut line = Vec::new();
    let mut skipping = false;
    while let Ok(buf) = reader.fill_buf().await {
        if buf.is_empty() {
            if !line.is_empty() && !skipping {
                broker.publish(source, &format!("{prefix}{}", String::from_utf8_lossy(&line)));
            }
            break;
        }
        let newline = buf.iter().position(|&b| b == b'\n');
        let chunk = &buf[..newline.unwrap_or(buf.len())];
        if !skipping {
            line.extend_from_slice(chunk);
            if line.len() > MAX_LINE_BYTES {
                line.clear();
                skipping = true;
                broker.publish(source, &format!("{prefix}[line over {MAX_LINE_BYTES} bytes dropped]"));
            }
        }
        let consumed = chunk.len() + usize::from(newline.is_some());
        reader.consume(consumed);
        if newline.is_some() {
            if !skipping {
                broker.publish(source, &format!("{prefix}{}", String::from_utf8_lossy(&line)));
            }
            line.clear();
            skipping = false;
        }
    }
}

/// Follow the daemon unit's journal into the `daemon` source for as long as
/// the controller runs. `--user-unit` reads the system journal, where user
/// services log when the user journal isn't persistent (the Jetson default).
pub async fn follow_journal(broker: Arc<LogBroker>, unit: String) {
    let child = tokio::process::Command::new("journalctl")
        .args([&format!("--user-unit={unit}"), "-f", "-n", "200", "-o", "cat"])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn();
    let mut child = match child {
        Ok(child) => child,
        Err(error) => {
            tracing::warn!(%error, "could not follow the {unit} journal");
            broker.set_status("daemon", "unavailable");
            return;
        }
    };
    broker.set_status("daemon", "live");
    let stdout = BufReader::new(child.stdout.take().expect("piped stdout"));
    let stderr = BufReader::new(child.stderr.take().expect("piped stderr"));
    // journalctl reports problems such as missing journal permissions on
    // stderr while it keeps following, so those lines are shown too.
    tokio::join!(
        pump_lines(stdout, &broker, "daemon", ""),
        pump_lines(stderr, &broker, "daemon", "journalctl: "),
    );
    let failed = child.wait().await.map_or(true, |status| !status.success());
    broker.set_status("daemon", if failed { "unavailable" } else { "stopped" });
}

/// Tracing layer feeding the `controller` source and `controller.log`.
pub struct ControllerLogLayer {
    broker: Arc<LogBroker>,
    file: Option<Mutex<std::fs::File>>,
}

impl ControllerLogLayer {
    pub fn new(broker: Arc<LogBroker>, path: Option<PathBuf>) -> Self {
        let file = path.and_then(|path| {
            std::fs::create_dir_all(path.parent()?).ok()?;
            std::fs::OpenOptions::new().create(true).append(true).open(path).ok().map(Mutex::new)
        });
        Self { broker, file }
    }
}

#[derive(Default)]
struct MessageVisitor {
    message: String,
    fields: String,
}

impl Visit for MessageVisitor {
    fn record_debug(&mut self, field: &Field, value: &dyn std::fmt::Debug) {
        if field.name() == "message" {
            self.message = format!("{value:?}");
        } else {
            self.fields.push_str(&format!(" {}={value:?}", field.name()));
        }
    }

    fn record_str(&mut self, field: &Field, value: &str) {
        if field.name() == "message" {
            self.message = value.to_string();
        } else {
            self.fields.push_str(&format!(" {}={value}", field.name()));
        }
    }
}

impl<S: tracing::Subscriber> Layer<S> for ControllerLogLayer {
    fn on_event(&self, event: &tracing::Event<'_>, _ctx: Context<'_, S>) {
        let mut visitor = MessageVisitor::default();
        event.record(&mut visitor);
        let meta = event.metadata();
        let line = format!(
            "{} {:<8} {}: {}{}",
            chrono::Local::now().format("%H:%M:%S"),
            meta.level().as_str(),
            meta.target(),
            visitor.message,
            visitor.fields
        );
        if let Some(file) = &self.file
            && let Ok(mut file) = file.lock()
        {
            let _ = writeln!(file, "{line}");
        }
        self.broker.publish("controller", &line);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ring_is_bounded_and_replay_is_the_tail() {
        let broker = LogBroker::new();
        for i in 0..(RING_SIZE + 10) {
            broker.publish("daemon", &format!("line {i}"));
        }
        let snapshot = broker.subscribe("daemon");
        assert_eq!(snapshot.lines.len(), REPLAY_LINES);
        assert_eq!(snapshot.lines.last().unwrap(), &format!("line {}", RING_SIZE + 9));
        assert_eq!(snapshot.status, "live");
    }

    #[tokio::test]
    async fn pump_drops_overlong_lines_whole() {
        let broker = LogBroker::new();
        let long = "x".repeat(MAX_LINE_BYTES + 1);
        let input = format!("first\n{long}\nlast\npartial");
        pump_lines(BufReader::new(input.as_bytes()), &broker, "daemon", "").await;
        let lines = broker.subscribe("daemon").lines;
        assert_eq!(lines, vec!["first", &format!("[line over {MAX_LINE_BYTES} bytes dropped]"), "last", "partial"]);
    }
}
