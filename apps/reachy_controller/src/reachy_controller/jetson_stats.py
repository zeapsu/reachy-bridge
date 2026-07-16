"""Jetson host metrics collection."""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any


def _memory() -> tuple[float, float]:
    """Return (used_gb, total_gb) from /proc/meminfo."""
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0])
    except (OSError, UnicodeError, ValueError):
        return 0.0, 0.0
    total = values.get("MemTotal", 0) / 1024**2
    available = values.get("MemAvailable", 0) / 1024**2
    return round(total - available, 2), round(total, 2)


def _cpu_percent() -> float | None:
    """Return CPU load percentage based on load average."""
    try:
        load = os.getloadavg()[0]
        cores = os.cpu_count() or 1
        return round(min(100.0, load / cores * 100), 1)
    except OSError:
        return None


def _read_text(path: str) -> str | None:
    """Read text file and return stripped content or None on error."""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def _gpu_percent() -> float | None:
    """Return GPU load percentage from sysfs."""
    for path in (
        "/sys/devices/platform/17000000.gpu/load",
        "/sys/devices/gpu.0/load",
    ):
        value = _read_text(path)
        if value:
            try:
                return round(float(value) / 10, 1)
            except ValueError:
                pass
    return None


def _temperature_c() -> float | None:
    """Return maximum temperature in Celsius from thermal zones."""
    temperatures: list[float] = []
    thermal_root = Path("/sys/class/thermal")
    try:
        zones = thermal_root.glob("thermal_zone*/temp")
        for path in zones:
            raw = path.read_text(encoding="utf-8").strip()
            value = float(raw)
            temperatures.append(value / 1000 if value > 1000 else value)
    except (OSError, UnicodeError, ValueError):
        return None
    return round(max(temperatures), 1) if temperatures else None


def collect_host_metrics() -> dict[str, Any]:
    """Collect Jetson host metrics: CPU, GPU, memory, and temperature."""
    used_gb, total_gb = _memory()
    return {
        "hostname": socket.gethostname(),
        "cpu_percent": _cpu_percent(),
        "gpu_percent": _gpu_percent(),
        "temperature_c": _temperature_c(),
        "memory_used_gb": used_gb,
        "memory_total_gb": total_gb,
    }


def _find_reachy_audio_card() -> int | None:
    """Return ALSA card number for Reachy Mini Audio by searching /proc/asound/cards."""
    try:
        with open("/proc/asound/cards") as f:
            lines = f.readlines()
    except OSError:
        return None

    card_number = None
    card_text = []
    for line in lines:
        stripped = line.lstrip()
        number, separator, remainder = stripped.partition(" ")
        if separator and number.isdigit() and remainder.lstrip().startswith("["):
            if card_number is not None:
                description = " ".join(card_text).casefold()
                if "reachy mini audio" in description or "pollen" in description:
                    return card_number
            card_number = int(number)
            card_text = [remainder]
        elif card_number is not None:
            card_text.append(line)

    if card_number is not None:
        description = " ".join(card_text).casefold()
        if "reachy mini audio" in description or "pollen" in description:
            return card_number
    return None


def _parse_alsa_pcm_volume(card: int) -> int | None:
    """Run 'amixer -c <card> sget PCM,1' and extract [NN%]."""
    import subprocess
    try:
        result = subprocess.run(
            ["amixer", "-c", str(card), "sget", "PCM,1"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        import re
        match = re.search(r'\[(\d+)%\]', result.stdout)
        if match:
            return int(match.group(1))
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, OSError):
        return None


def _parse_pipewire_sink_volume() -> float | None:
    """Run 'wpctl status' and extract default sink volume."""
    import subprocess
    try:
        result = subprocess.run(
            ["wpctl", "status"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        import re
        match = re.search(r'\*\s+\d+\..*?\[vol:\s*([\d.]+)', result.stdout)
        if match:
            return float(match.group(1))
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, OSError):
        return None


def _separate_cpu_gpu_temps() -> tuple[float | None, float | None]:
    """Read /sys/class/thermal/thermal_zone*/type and separate CPU/GPU temps."""
    cpu_temps = []
    gpu_temps = []
    try:
        thermal_root = Path("/sys/class/thermal")
        for zone_dir in sorted(thermal_root.glob("thermal_zone*")):
            type_file = zone_dir / "type"
            temp_file = zone_dir / "temp"
            try:
                zone_type = type_file.read_text(encoding="utf-8").strip().lower()
                temp_raw = temp_file.read_text(encoding="utf-8").strip()
                temp_val = float(temp_raw)
                temp_c = temp_val / 1000 if temp_val > 1000 else temp_val

                if "cpu" in zone_type:
                    cpu_temps.append(temp_c)
                elif "gpu" in zone_type:
                    gpu_temps.append(temp_c)
            except (OSError, ValueError, UnicodeError):
                pass
    except OSError:
        pass

    cpu_temp = round(max(cpu_temps), 1) if cpu_temps else None
    gpu_temp = round(max(gpu_temps), 1) if gpu_temps else None
    return cpu_temp, gpu_temp


def collect_jetson_stats() -> dict[str, Any]:
    """Collect comprehensive Jetson system stats: CPU, GPU, memory, temps, audio routing."""
    used_gb, total_gb = _memory()
    cpu_temp, gpu_temp = _separate_cpu_gpu_temps()
    alsa_card = _find_reachy_audio_card()
    alsa_pcm_vol = None
    if alsa_card is not None:
        alsa_pcm_vol = _parse_alsa_pcm_volume(alsa_card)

    return {
        "cpu_percent": _cpu_percent(),
        "memory_used_mb": round(used_gb * 1024, 2),
        "memory_total_mb": round(total_gb * 1024, 2),
        "gpu_percent": _gpu_percent(),
        "cpu_temp_c": cpu_temp,
        "gpu_temp_c": gpu_temp,
        "alsa_card": alsa_card,
        "alsa_pcm_volume": alsa_pcm_vol,
        "pipewire_sink_volume": _parse_pipewire_sink_volume(),
    }
