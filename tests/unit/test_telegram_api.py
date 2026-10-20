"""Bot API client: request building, token masking, message splitting, file download. No network:
every request goes through `httpx.MockTransport`."""

from __future__ import annotations

import json

import httpx
import pytest

from jarvis.interfaces.telegram_api import (
    MAX_MESSAGE_CHARS,
    TelegramApi,
    TelegramApiError,
    clean_outgoing,
    inline_keyboard,
    looks_like_token,
    mask_token,
    split_message,
)

TOKEN = "123456789:AAHfakeTOKENvalue_for-tests-0123456789"


def make_api(handler) -> tuple[TelegramApi, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return TelegramApi(TOKEN, transport=httpx.MockTransport(wrapped)), seen


def ok(result) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


# --- request building --------------------------------------------------------------------------


def test_send_message_posts_json_to_the_token_url():
    api, seen = make_api(lambda r: ok({"message_id": 7, "chat": {"id": 5}}))
    result = api.send_message(5, "hello", reply_markup={"inline_keyboard": []})
    assert result["message_id"] == 7
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    body = json.loads(request.content)
    assert body == {"chat_id": 5, "text": "hello", "reply_markup": {"inline_keyboard": []}}


def test_get_updates_sends_offset_timeout_and_allowed_updates():
    api, seen = make_api(lambda r: ok([{"update_id": 1}]))
    updates = api.get_updates(41, timeout=25)
    assert updates == [{"update_id": 1}]
    body = json.loads(seen[0].content)
    assert body == {"offset": 41, "timeout": 25, "allowed_updates": ["message", "callback_query"]}
    assert seen[0].url.path.endswith("/getUpdates")


def test_none_params_are_dropped_and_other_methods_build():
    api, seen = make_api(lambda r: ok([]) if r.url.path.endswith("/getUpdates") else ok(True))
    assert api.get_updates(None, timeout=5) == []
    assert "offset" not in json.loads(seen[-1].content)
    api.send_chat_action(5)
    assert json.loads(seen[-1].content) == {"chat_id": 5, "action": "typing"}
    api.answer_callback_query("cq1", "done")
    assert json.loads(seen[-1].content) == {"callback_query_id": "cq1", "text": "done"}
    api.edit_message_text(5, 7, "new")
    assert json.loads(seen[-1].content) == {"chat_id": 5, "message_id": 7, "text": "new"}
    api.get_me()
    assert seen[-1].url.path.endswith("/getMe")


# --- errors and token masking ------------------------------------------------------------------


def test_api_error_response_is_raised_without_the_token():
    api, _ = make_api(lambda r: httpx.Response(401, json={"ok": False, "error_code": 401, "description": f"Unauthorized {r.url}"}))
    with pytest.raises(TelegramApiError) as exc_info:
        api.get_me()
    message = str(exc_info.value)
    assert TOKEN not in message and "401" in message and "getMe" in message
    assert exc_info.value.status == 401


def test_transport_error_message_is_masked_and_not_chained():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}", request=request)

    api, _ = make_api(boom)
    with pytest.raises(TelegramApiError) as exc_info:
        api.get_updates(None, timeout=1)
    assert TOKEN not in str(exc_info.value)
    assert "bot***" in str(exc_info.value)
    assert exc_info.value.__cause__ is None and exc_info.value.__suppress_context__


def test_non_json_body_and_retry_after():
    api, _ = make_api(lambda r: httpx.Response(502, text="<html>bad gateway</html>"))
    with pytest.raises(TelegramApiError, match="non-JSON"):
        api.get_me()
    api, _ = make_api(
        lambda r: httpx.Response(429, json={"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 3}})
    )
    with pytest.raises(TelegramApiError) as exc_info:
        api.get_me()
    assert exc_info.value.retry_after == 3.0


def test_mask_token_helpers():
    assert mask_token(f"https://api.telegram.org/bot{TOKEN}/getMe", TOKEN) == "https://api.telegram.org/bot***/getMe"
    assert mask_token(f"url bot{TOKEN}/x") == "url bot***/x"  # even without knowing the token
    assert looks_like_token(TOKEN) and not looks_like_token("hello") and not looks_like_token("")


def test_empty_token_is_refused():
    with pytest.raises(TelegramApiError):
        TelegramApi("  ")


# --- outgoing text -------------------------------------------------------------------------------


def test_split_message_prefers_newlines_then_spaces_then_hard_cuts():
    assert split_message("") == [] and split_message("   \n ") == []
    assert split_message("short") == ["short"]
    text = "a" * 3000 + "\n" + "b" * 3000
    chunks = split_message(text)
    assert chunks == ["a" * 3000, "b" * 3000]
    text = "a" * 3000 + " " + "b" * 3000
    assert split_message(text) == ["a" * 3000, "b" * 3000]
    chunks = split_message("x" * 9000)
    assert [len(c) for c in chunks] == [4096, 4096, 808]
    assert all(len(c) <= MAX_MESSAGE_CHARS for c in split_message("word " * 5000))


def test_outgoing_text_is_stripped_of_escapes_and_controls():
    assert clean_outgoing("hi\x1b[31m there\x07") == "hi there"
    assert split_message("a\x1b]0;evil\x07b") == ["ab"]


def test_send_message_refuses_oversized_and_empty_text():
    api, seen = make_api(lambda r: ok({"message_id": 1}))
    with pytest.raises(TelegramApiError, match="4096"):
        api.send_message(1, "x" * 4097)
    with pytest.raises(TelegramApiError, match="empty"):
        api.send_message(1, "   ")
    assert seen == []
    sent = api.send_long(1, "y" * 5000)
    assert len(sent) == 2 and len(json.loads(seen[0].content)["text"]) == 4096


def test_inline_keyboard_limits_callback_data():
    markup = inline_keyboard([[("Onayla", "cf:abcdefabcdef:y"), ("Reddet", "cf:abcdefabcdef:n")]])
    assert markup == {
        "inline_keyboard": [
            [{"text": "Onayla", "callback_data": "cf:abcdefabcdef:y"}, {"text": "Reddet", "callback_data": "cf:abcdefabcdef:n"}]
        ]
    }
    with pytest.raises(ValueError):
        inline_keyboard([[("x", "d" * 65)]])


# --- files ---------------------------------------------------------------------------------------


def test_get_file_and_download_use_the_file_url():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return ok({"file_id": "f1", "file_path": "voice/file_1.oga", "file_size": 3})
        if request.method == "GET":
            return httpx.Response(200, content=b"OGG", headers={"content-length": "3"})
        return httpx.Response(404, json={"ok": False, "description": "no"})

    api, seen = make_api(handler)
    info = api.get_file("f1")
    assert json.loads(seen[0].content) == {"file_id": "f1"}
    data = api.download_file(info["file_path"])
    assert data == b"OGG"
    assert str(seen[1].url) == f"https://api.telegram.org/file/bot{TOKEN}/voice/file_1.oga"


def test_download_rejects_files_over_the_limit():
    api, _ = make_api(lambda r: httpx.Response(200, content=b"x" * 50, headers={"content-length": "50"}))
    with pytest.raises(TelegramApiError, match="exceeds"):
        api.download_file("a/b", max_bytes=10)
    api, _ = make_api(lambda r: httpx.Response(200, content=iter([b"x" * 30, b"x" * 30])))  # chunked: streamed cap
    with pytest.raises(TelegramApiError, match="more than"):
        api.download_file("a/b", max_bytes=10)
    api, _ = make_api(lambda r: httpx.Response(404, content=b""))
    with pytest.raises(TelegramApiError, match="404"):
        api.download_file("a/b")
