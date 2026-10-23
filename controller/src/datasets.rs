//! Cached recorded-move dataset lists.
//!
//! The daemon's list route is an `async def` that loads the whole dataset
//! (Hugging Face snapshot and every move file) synchronously on its event
//! loop. A cold load takes about 5 s on the Jetson, and the loop runs the
//! moves too: a list fetched during the wake-up move froze the head mid-way,
//! then the trajectory jumped to catch up. The lists don't change while the
//! daemon runs, so they are cached here and on disk, and the dashboard's
//! datasets are fetched at startup only while the robot is off.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;

use reqwest::Method;
use serde_json::Value;

use crate::daemon::{backend_up, DaemonError};
use crate::state::Shared;

/// The datasets the dashboard shows.
pub const DASHBOARD_DATASETS: [&str; 2] =
    ["pollen-robotics/reachy-mini-emotions-library", "pollen-robotics/reachy-mini-dances-library"];

/// A cold load takes seconds; don't give up on it at the default 2 s.
const FETCH_TIMEOUT: Duration = Duration::from_secs(30);
const PREFETCH_RETRY: Duration = Duration::from_secs(10);

pub struct DatasetCache {
    path: PathBuf,
    lists: Mutex<HashMap<String, Value>>,
    /// One daemon fetch at a time, so concurrent tabs don't each load it.
    fetching: tokio::sync::Mutex<()>,
}

impl DatasetCache {
    /// Load the cache file at `path` (missing or unreadable: empty).
    pub fn new(path: PathBuf) -> Self {
        let lists = std::fs::read_to_string(&path)
            .ok()
            .and_then(|text| serde_json::from_str::<HashMap<String, Value>>(&text).ok())
            .unwrap_or_default();
        Self { path, lists: Mutex::new(lists), fetching: tokio::sync::Mutex::new(()) }
    }

    pub fn get(&self, dataset: &str) -> Option<Value> {
        self.lists.lock().unwrap().get(dataset).cloned()
    }

    fn insert(&self, dataset: &str, moves: Value) {
        let snapshot = {
            let mut lists = self.lists.lock().unwrap();
            lists.insert(dataset.to_string(), moves);
            serde_json::to_string(&*lists).unwrap_or_default()
        };
        let tmp = self.path.with_extension("tmp");
        let written = self
            .path
            .parent()
            .map_or(Ok(()), std::fs::create_dir_all)
            .and_then(|()| std::fs::write(&tmp, snapshot))
            .and_then(|()| std::fs::rename(&tmp, &self.path));
        if let Err(error) = written {
            tracing::warn!(%error, "could not save the dataset cache");
        }
    }

    /// The dataset's move names: cached, or fetched once from the daemon.
    /// Only a list answer is cached; errors pass through.
    pub async fn list(&self, state: &Shared, dataset: &str) -> Result<Value, DaemonError> {
        if let Some(moves) = self.get(dataset) {
            return Ok(moves);
        }
        let _fetching = self.fetching.lock().await;
        if let Some(moves) = self.get(dataset) {
            return Ok(moves);
        }
        let path = format!("move/recorded-move-datasets/list/{dataset}");
        let moves = state.daemon.call(Method::GET, &path, &[], None, Some(FETCH_TIMEOUT)).await?;
        if moves.is_array() {
            self.insert(dataset, moves.clone());
        }
        Ok(moves)
    }
}

/// Fetch the dashboard's datasets while the robot is off, retrying until
/// they are cached. Holds the power lock during each fetch, so a power-on
/// waits for it instead of moving while the daemon's loop is blocked.
pub async fn prefetch(state: Shared) {
    let missing = || -> Vec<&str> {
        DASHBOARD_DATASETS.into_iter().filter(|d| state.datasets.get(d).is_none()).collect()
    };
    while !missing().is_empty() {
        {
            let _power = state.power_lock.lock().await;
            let off = state.daemon.status().await.is_some_and(|s| !backend_up(&s));
            if off {
                for dataset in missing() {
                    if let Err(error) = state.datasets.list(&state, dataset).await {
                        tracing::info!(?error, "dataset {dataset} not cached yet");
                    }
                }
            }
        }
        if !missing().is_empty() {
            tokio::time::sleep(PREFETCH_RETRY).await;
        }
    }
}
