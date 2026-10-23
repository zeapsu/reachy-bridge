//! Host metrics for the system panel: CPU, GPU, memory, temperatures, and the
//! Reachy audio routing (ALSA card, PipeWire sink volume).

use std::path::Path;
use std::io::Read;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

fn read_trimmed(path: impl AsRef<Path>) -> Option<String> {
    std::fs::read_to_string(path).ok().map(|s| s.trim().to_string())
}

fn round(value: f64, places: i32) -> f64 {
    let factor = 10f64.powi(places);
    (value * factor).round() / factor
}

/// (used, total) in GiB from `/proc/meminfo`.
fn memory_gb() -> (f64, f64) {
    let Some(text) = read_trimmed("/proc/meminfo") else { return (0.0, 0.0) };
    let field = |key: &str| {
        text.lines()
            .find_map(|line| line.strip_prefix(key)?.strip_prefix(':'))
            .and_then(|rest| rest.split_whitespace().next()?.parse::<f64>().ok())
            .unwrap_or(0.0)
    };
    let gib = 1024.0 * 1024.0;
    let total = field("MemTotal") / gib;
    let available = field("MemAvailable") / gib;
    (round(total - available, 2), round(total, 2))
}

/// One-minute load average as a share of the cores, capped at 100.
fn cpu_percent() -> Option<f64> {
    let load: f64 = read_trimmed("/proc/loadavg")?.split_whitespace().next()?.parse().ok()?;
    let cores = std::thread::available_parallelism().map_or(1, |n| n.get()) as f64;
    Some(round((load / cores * 100.0).min(100.0), 1))
}

fn gpu_percent() -> Option<f64> {
    ["/sys/devices/platform/17000000.gpu/load", "/sys/devices/gpu.0/load"]
        .iter()
        .find_map(|path| read_trimmed(path)?.parse::<f64>().ok())
        .map(|load| round(load / 10.0, 1))
}

/// Highest CPU and GPU thermal-zone temperatures in °C.
fn cpu_gpu_temps() -> (Option<f64>, Option<f64>) {
    let (mut cpu, mut gpu): (Option<f64>, Option<f64>) = (None, None);
    let Ok(zones) = std::fs::read_dir("/sys/class/thermal") else { return (None, None) };
    for zone in zones.flatten() {
        if !zone.file_name().to_string_lossy().starts_with("thermal_zone") {
            continue;
        }
        let (Some(kind), Some(raw)) = (read_trimmed(zone.path().join("type")), read_trimmed(zone.path().join("temp")))
        else {
            continue;
        };
        let Ok(value) = raw.parse::<f64>() else { continue };
        let celsius = if value > 1000.0 { value / 1000.0 } else { value };
        let kind = kind.to_lowercase();
        let slot = if kind.contains("cpu") {
            &mut cpu
        } else if kind.contains("gpu") {
            &mut gpu
        } else {
            continue;
        };
        *slot = Some(slot.map_or(celsius, |t| t.max(celsius)));
    }
    (cpu.map(|t| round(t, 1)), gpu.map(|t| round(t, 1)))
}

/// ALSA card number of the Reachy Mini Audio device, from `/proc/asound/cards`.
pub fn reachy_audio_card(cards: &str) -> Option<u32> {
    let mut current: Option<(u32, String)> = None;
    let matches = |text: &str| {
        let text = text.to_lowercase();
        text.contains("reachy mini audio") || text.contains("pollen")
    };
    for line in cards.lines() {
        let trimmed = line.trim_start();
        let header = trimmed
            .split_once(' ')
            .filter(|(number, rest)| number.chars().all(|c| c.is_ascii_digit()) && rest.trim_start().starts_with('['));
        if let Some((number, rest)) = header {
            if let Some((card, text)) = current.take()
                && matches(&text)
            {
                return Some(card);
            }
            current = number.parse().ok().map(|n| (n, rest.to_string()));
        } else if let Some((_, text)) = current.as_mut() {
            text.push(' ');
            text.push_str(line);
        }
    }
    current.filter(|(_, text)| matches(text)).map(|(card, _)| card)
}

const COMMAND_TIMEOUT: Duration = Duration::from_secs(5);

/// Stdout of a short command, or `None` if it fails or runs past 5 s (a
/// wedged PipeWire can hang `wpctl`). Stdout is read on its own thread so a
/// full pipe can't stall the child.
fn command_stdout(program: &str, args: &[&str]) -> Option<String> {
    let mut child = Command::new(program)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let mut stdout = child.stdout.take()?;
    let reader = std::thread::spawn(move || {
        let mut text = String::new();
        let _ = stdout.read_to_string(&mut text);
        text
    });
    let deadline = Instant::now() + COMMAND_TIMEOUT;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(20)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    };
    let text = reader.join().ok()?;
    status.success().then_some(text)
}

/// First `[NN%]` in `amixer sget` output.
pub fn parse_percent(text: &str) -> Option<u32> {
    let start = text.find('[')?;
    let rest = &text[start + 1..];
    let end = rest.find("%]")?;
    rest[..end].parse().ok().or_else(|| parse_percent(&rest[end..]))
}

/// Volume of the default sink (`*` row) in `wpctl status`.
pub fn parse_default_sink_volume(text: &str) -> Option<f64> {
    text.lines()
        .filter(|line| line.contains('*'))
        .find_map(|line| line.split_once("[vol:")?.1.split_whitespace().next()?.trim_end_matches(']').parse().ok())
}

fn max_temperature() -> Option<f64> {
    let zones = std::fs::read_dir("/sys/class/thermal").ok()?;
    zones
        .flatten()
        .filter(|zone| zone.file_name().to_string_lossy().starts_with("thermal_zone"))
        .filter_map(|zone| read_trimmed(zone.path().join("temp"))?.parse::<f64>().ok())
        .map(|value| if value > 1000.0 { value / 1000.0 } else { value })
        .reduce(f64::max)
        .map(|t| round(t, 1))
}

/// The host block of `/api/telemetry`.
pub fn collect_host_metrics() -> Value {
    let (used, total) = memory_gb();
    json!({
        "hostname": read_trimmed("/proc/sys/kernel/hostname"),
        "cpu_percent": cpu_percent(),
        "gpu_percent": gpu_percent(),
        "temperature_c": max_temperature(),
        "memory_used_gb": used,
        "memory_total_gb": total,
    })
}

pub fn collect_stats() -> Value {
    let (used, total) = memory_gb();
    let (cpu_temp, gpu_temp) = cpu_gpu_temps();
    let card = read_trimmed("/proc/asound/cards").and_then(|cards| reachy_audio_card(&cards));
    let pcm = card.and_then(|c| parse_percent(&command_stdout("amixer", &["-c", &c.to_string(), "sget", "PCM,1"])?));
    let sink = command_stdout("wpctl", &["status"]).and_then(|s| parse_default_sink_volume(&s));
    json!({
        "cpu_percent": cpu_percent(),
        "memory_used_mb": round(used * 1024.0, 2),
        "memory_total_mb": round(total * 1024.0, 2),
        "gpu_percent": gpu_percent(),
        "cpu_temp_c": cpu_temp,
        "gpu_temp_c": gpu_temp,
        "alsa_card": card,
        "alsa_pcm_volume": pcm,
        "pipewire_sink_volume": sink,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const CARDS: &str = " 0 [HDA            ]: tegra-hda - NVIDIA Jetson HDA\n                      NVIDIA Jetson HDA at 0x3518000\n 2 [Audio          ]: USB-Audio - Reachy Mini Audio\n                      Pollen Robotics Reachy Mini Audio at usb-1\n";

    #[test]
    fn finds_the_reachy_card() {
        assert_eq!(reachy_audio_card(CARDS), Some(2));
        assert_eq!(reachy_audio_card(" 0 [HDA ]: tegra - Jetson\n"), None);
    }

    #[test]
    fn parses_amixer_and_wpctl() {
        assert_eq!(parse_percent("  Front Left: Playback 50 [78%] [on]"), Some(78));
        let wpctl = "Sinks:\n │      45. Other  [vol: 0.40]\n │  *   52. Reachy Mini Audio Analog Stereo [vol: 0.85]\n";
        assert_eq!(parse_default_sink_volume(wpctl), Some(0.85));
    }
}
