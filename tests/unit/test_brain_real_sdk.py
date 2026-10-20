"""Drives the real SDK client (no network reachable) through the Brain's retry path."""

import anthropic
import pytest

from jarvis.brain import Brain
from jarvis.exceptions import JarvisAPIError
from jarvis.observability.timing import TurnTimer
from jarvis.observability.usage import UsageTracker
from jarvis.tools.registry import ToolRegistry
from tests.conftest import RecordingSink


def test_real_client_connection_errors_are_retried_then_raised(config, db, logs):
    # Port 9 (discard) is closed on macOS, so the SDK raises APIConnectionError at once.
    client = anthropic.Anthropic(api_key="sk-ant-test", base_url="http://127.0.0.1:9", max_retries=0, timeout=2.0)
    sleeps = []
    brain = Brain(client, config, ToolRegistry.discover(), db, UsageTracker(config, db), sleep=sleeps.append)
    db.start_session("s", "m", "d")
    db.start_turn("t", "s", "hi")
    sink = RecordingSink()

    with pytest.raises(JarvisAPIError) as exc:
        brain.run_turn(system="sys", messages=[{"role": "user", "content": "hi"}], sink=sink, turn_id="t", timer=TurnTimer("t", db))
    assert exc.value.attempts == config.loop.api_max_attempts
    assert exc.value.status_code is None and exc.value.error_type == "APIConnectionError"
    assert len(sleeps) == config.loop.api_max_attempts - 1
    rows = db.conn.execute("SELECT status FROM api_calls").fetchall()
    assert len(rows) == config.loop.api_max_attempts and all(r["status"].startswith("error:") for r in rows)
