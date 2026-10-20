import json
import logging
from datetime import datetime, timedelta, timezone

from jarvis.observability import logging_setup
from jarvis.observability.logging_setup import prune_jsonl, redact


def test_redaction_masks_secrets_and_emails():
    text = (
        "key sk-ant-api03-abcdefghijklmnop token ya29.a0AfH6SMB-xyz refresh 1//0gabcdefghijkl "
        "Authorization: Bearer abcdefghijklmnop mail kerem@example.com"
    )
    out = redact(text)
    assert "sk-ant-api03" not in out and "sk-ant-***" in out
    assert "ya29.a0" not in out and "1//0gabc" not in out
    assert "Bearer abcdef" not in out
    assert "kerem@example.com" not in out and "***@redacted" in out
    token = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1"  # Telegram bot token shape
    assert token not in redact(f"https://api.telegram.org/bot{token}/getUpdates") and "***:***" in redact(token)


def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_three_channels_carry_ids(logs):
    logging_setup.set_session_id("sess1")
    logging_setup.set_turn_id("turn1")
    logging.getLogger("jarvis.test").info("hello %s", "world", extra={"data": {"key": "sk-ant-secret1234567890"}})
    logging_setup.audit({"tool": "get_time", "decision": "ran"})
    logging_setup.usage({"model": "m", "input_tokens": 1})

    app = _lines(logs / "jarvis.log")
    assert app[-1]["msg"] == "hello world"
    assert app[-1]["session_id"] == "sess1" and app[-1]["turn_id"] == "turn1"
    assert app[-1]["data"]["key"] == "sk-ant-***"

    audit = _lines(logs / "audit.jsonl")
    assert audit[-1]["tool"] == "get_time" and audit[-1]["turn_id"] == "turn1" and "ts" in audit[-1]

    usage = _lines(logs / "usage.jsonl")
    assert usage[-1]["model"] == "m" and usage[-1]["session_id"] == "sess1"


def test_audit_channel_does_not_leak_into_app_log(logs):
    logging_setup.audit({"tool": "x"})
    app = _lines(logs / "jarvis.log")
    assert all(line.get("logger") != "jarvis.audit" for line in app)


def test_prune_jsonl_keeps_recent_lines(tmp_path):
    path = tmp_path / "audit.jsonl"
    old = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    new = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    path.write_text(json.dumps({"ts": old, "n": 1}) + "\n" + json.dumps({"ts": new, "n": 2}) + "\nnot json\n")
    removed = prune_jsonl(path, retention_days=90)
    assert removed == 1
    remaining = path.read_text().splitlines()
    assert len(remaining) == 2 and '"n": 2' in remaining[0] and remaining[1] == "not json"
