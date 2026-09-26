"""The shared OpenRouter client, on a fake transport (no network)."""

import urllib.error

import pytest

from tokeneyezed.openrouter import OpenRouterClient, OpenRouterError


def body(text):
    return {"choices": [{"message": {"content": text}}]}


def http_error(code):
    return urllib.error.HTTPError("https://x", code, "err", {}, None)


def scripted(*responses):
    calls = []

    def transport(payload, key):
        calls.append((payload, key))
        item = responses[min(len(calls), len(responses)) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    return transport, calls


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")


def test_request_shape_and_json_mode():
    transport, calls = scripted(body("hi"))
    reply = OpenRouterClient(transport=transport).chat(
        model="m", messages=[{"role": "user", "content": "x"}], max_tokens=5, json_mode=True
    )
    payload, key = calls[0]
    assert reply == "hi" and key == "k"
    assert payload["model"] == "m" and payload["max_tokens"] == 5
    assert payload["response_format"] == {"type": "json_object"}


def test_no_json_mode_by_default():
    transport, calls = scripted(body("hi"))
    OpenRouterClient(transport=transport).chat(model="m", messages=[], max_tokens=5)
    assert "response_format" not in calls[0][0]


def test_retries_rate_limits_server_and_network_errors_with_backoff():
    transport, calls = scripted(
        http_error(429), http_error(503), urllib.error.URLError("down"), body("ok")
    )
    sleeps = []
    client = OpenRouterClient(transport=transport, sleep=sleeps.append)
    assert client.chat(model="m", messages=[], max_tokens=5) == "ok"
    assert sleeps == [1, 2, 4] and len(calls) == 4


def test_gives_up_after_retries():
    transport, calls = scripted(http_error(500))
    client = OpenRouterClient(transport=transport, sleep=lambda s: None, max_retries=2)
    with pytest.raises(OpenRouterError, match="after 2 retries"):
        client.chat(model="m", messages=[], max_tokens=5)
    assert len(calls) == 3


@pytest.mark.parametrize("code", [400, 401, 404])
def test_client_errors_are_not_retried(code):
    transport, calls = scripted(http_error(code))
    with pytest.raises(OpenRouterError, match=f"HTTP {code}"):
        OpenRouterClient(transport=transport, sleep=lambda s: None).chat(
            model="m", messages=[], max_tokens=5
        )
    assert len(calls) == 1


@pytest.mark.parametrize("bad", [None, {}, {"choices": []}, body(""), body(None)])
def test_unusable_responses_raise(bad):
    transport, _ = scripted(bad)
    with pytest.raises(OpenRouterError):
        OpenRouterClient(transport=transport).chat(model="m", messages=[], max_tokens=5)


def test_missing_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    transport, calls = scripted(body("x"))
    with pytest.raises(OpenRouterError, match="OPENROUTER_API_KEY"):
        OpenRouterClient(transport=transport).chat(model="m", messages=[], max_tokens=5)
    assert calls == []
