"""The speed test through macOS's built-in `networkQuality`: download, upload,
responsiveness (RPM) and the idle round-trip time, parsed from its `-c` JSON output.

`networkQuality -c` prints exactly one JSON object on stdout; `-M` caps the runtime in seconds.
It takes about 20 seconds and uses data, so nothing calls it unless asked (or
`network.periodic_speedtest_hours > 0`). The runner is injectable; `parse_network_quality` is pure.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from jarvis.exceptions import ToolExecutionError
from jarvis.network.local import Runner, run_command

if TYPE_CHECKING:
    from jarvis.config import Config

BINARY = "networkQuality"
COMMAND_GRACE_S = 30.0  # subprocess timeout = -M plus this
RAW_KEYS = ("interface_name", "start_date", "end_date", "dl_flows", "ul_flows", "os_version", "test_endpoint", "other")


@dataclass
class SpeedResult:
    download_mbps: float | None
    upload_mbps: float | None
    responsiveness_rpm: float | None
    latency_ms: float | None  # base_rtt: idle round trip to the test endpoint
    interface: str | None = None
    duration_s: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)  # a trimmed copy of the JSON


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mbps(bits_per_second: Any) -> float | None:
    bps = _number(bits_per_second)
    return round(bps / 1_000_000, 1) if bps is not None else None


def parse_network_quality(text: str) -> SpeedResult:
    """`dl_throughput`/`ul_throughput` are bits per second, `responsiveness` RPM, `base_rtt` ms."""
    try:
        data = json.loads(text or "")
    except ValueError:
        raise ToolExecutionError("networkQuality printed no JSON (run it by hand to see why)") from None
    if not isinstance(data, dict) or "dl_throughput" not in data:
        raise ToolExecutionError("networkQuality output was not understood (no dl_throughput)")
    responsiveness = _number(data.get("responsiveness"))
    base_rtt = _number(data.get("base_rtt"))
    return SpeedResult(
        download_mbps=_mbps(data.get("dl_throughput")),
        upload_mbps=_mbps(data.get("ul_throughput")),
        responsiveness_rpm=round(responsiveness) if responsiveness is not None else None,
        latency_ms=round(base_rtt, 1) if base_rtt is not None else None,
        interface=str(data["interface_name"]) if data.get("interface_name") else None,
        raw={k: data[k] for k in RAW_KEYS if k in data},
    )


def speedtest_argv(config: "Config") -> list[str]:
    return [BINARY, "-c", "-M", str(config.network.speedtest_max_runtime_s)]


def speedtest_available(which: Callable[[str], str | None] = shutil.which) -> bool:
    return which(BINARY) is not None


def run_speed_test(config: "Config", *, run: Runner = run_command, which: Callable[[str], str | None] = shutil.which) -> SpeedResult:
    if not speedtest_available(which):
        raise ToolExecutionError(f"{BINARY} not found: the speed test needs macOS 12 or newer")
    started = time.perf_counter()
    result = run(speedtest_argv(config), config.network.speedtest_max_runtime_s + COMMAND_GRACE_S)
    if not result.ok and not result.stdout.strip():
        detail = (result.stderr or "").strip().splitlines()
        raise ToolExecutionError(f"{BINARY} failed (exit {result.returncode}): {detail[-1] if detail else 'no output'}"[:300])
    speed = parse_network_quality(result.stdout)
    speed.duration_s = round(time.perf_counter() - started, 1)
    return speed
