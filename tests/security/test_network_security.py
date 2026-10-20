"""Internet test security: hosts travel to ping as single argv elements (never a
shell), the history range is validated, stored details hold no raw command output, and no code
path uses sudo."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jarvis.exceptions import ToolValidationError
from jarvis.network import local, periodic, probes, speedtest
from jarvis.network.local import CommandResult
from jarvis.tools.base import ToolContext
from jarvis.tools.network import InternetHistory

REPO = Path(__file__).resolve().parents[2]
NETWORK_SOURCES = [*sorted((REPO / "src/jarvis/network").glob("*.py")), REPO / "src/jarvis/tools/network.py"]


class ArgvRecorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: float) -> CommandResult:
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv) and timeout > 0
        self.calls.append(list(argv))
        return CommandResult(1, "", "")


def test_hosts_reach_ping_as_single_argv_elements():
    recorder = ArgvRecorder()
    host = "1.1.1.1; rm -rf ~"  # would be rejected by NetworkConfig; the probe must still be safe
    probes.ping(host, recorder)
    assert recorder.calls == [[*probes.ping_argv(host)[:-1], host]]
    assert recorder.calls[0][-1] == host and len(recorder.calls[0]) == 10


def test_config_rejects_hosts_that_are_not_plain(config):
    from jarvis.config import NetworkConfig

    for bad in ["1.1.1.1 -f", "-c", "a;b", "$(id)", "a|b", "`id`"]:
        with pytest.raises(ValueError):
            NetworkConfig(public_check_hosts=[bad])
    assert NetworkConfig(dns_check_names=["Apple.COM"]).dns_check_names == ["apple.com"]


def test_history_range_is_validated(config, paths, db):
    ctx = ToolContext.build(config, paths)
    tool = InternetHistory(ctx)
    for bad in ["year", "day; DROP TABLE network_tests", "", 7, None]:
        with pytest.raises(ToolValidationError):
            tool.parse_args({"range": bad})
    assert tool.parse_args({"range": "month"}).range == "month"


def test_no_shell_no_sudo_in_network_sources():
    for path in NETWORK_SOURCES:
        text = path.read_text()
        assert re.search(r"[\"']sudo", text) is None, path  # never as a command or argv element
        assert "sudo " not in text.replace("without sudo", ""), path  # nor in a shell string
        assert "shell=True" not in text, path
        assert "os.system" not in text, path
    assert local.run_command.__module__ == "jarvis.network.local"


def test_every_command_has_a_timeout(config):
    """Each argv runs through a runner with a positive timeout; the speed test gets -M plus a grace."""
    recorder = ArgvRecorder()
    probes.run_diagnostics(config, run=recorder, resolver=lambda n, t: None, tcp=lambda h, p, t: False, wifi=lambda: None)
    assert recorder.calls and all(c[0] in {"route", "arp", "ifconfig", "networksetup", "ping"} for c in recorder.calls)
    with pytest.raises(Exception):
        speedtest.run_speed_test(config, run=recorder, which=lambda name: "/usr/bin/networkQuality")
    assert recorder.calls[-1] == ["networkQuality", "-c", "-M", "30"]
    assert periodic.tick(config, None, None) is None  # defaults: nothing runs, the db is not even touched


def test_detail_json_has_no_raw_command_output(config, db):
    from jarvis.network.diagnose import likely_cause
    from jarvis.network.store import NetworkStore

    marker = "PING-RAW-OUTPUT-MARKER"
    diag = probes.run_diagnostics(
        config, run=lambda a, t: CommandResult(0, marker * 50), resolver=lambda n, t: None, tcp=lambda h, p, t: False, wifi=lambda: None
    )
    NetworkStore(db).add("quick", diag, None, likely_cause(diag))
    row = NetworkStore(db).last()
    assert marker not in row["detail_json"] and len(row["detail_json"]) < 4000
    assert re.search(r"'|--", row["verdict"]) is None
