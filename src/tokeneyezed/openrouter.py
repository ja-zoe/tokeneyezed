"""The one OpenRouter client, shared by every model call in the harness (compactor, planner).

Retries rate limits (429), server errors (5xx), and network errors (including a dropped
connection and a non-JSON reply) with exponential backoff; any other HTTP error, or a response with
no usable content, raises OpenRouterError. Nothing else escapes, so callers need catch only that.
The transport is injectable so tests never touch the network.
"""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_RETRIES = 3

Transport = Callable[[dict[str, Any], str], dict[str, Any]]


class OpenRouterError(Exception):
    """The model call failed or returned nothing usable."""


def http_post(payload: dict[str, Any], key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


class OpenRouterClient:
    def __init__(
        self,
        transport: Transport = http_post,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = MAX_RETRIES,
        api_key: str | None = None,  # default: OPENROUTER_API_KEY, read at call time
    ) -> None:
        self._api_key = api_key
        self._transport = transport
        self._sleep = sleep
        self._max_retries = max_retries

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float = 0.0,
        json_mode: bool = False,
    ) -> str:
        """One chat completion; returns the message content."""
        key = self._api_key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise OpenRouterError("OPENROUTER_API_KEY is not set")
        payload: dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        for attempt in range(self._max_retries + 1):
            try:
                body = self._transport(payload, key)
            except urllib.error.HTTPError as err:
                if err.code != 429 and err.code < 500:
                    raise OpenRouterError(
                        f"OpenRouter rejected the request: HTTP {err.code}"
                    ) from err
            except (OSError, http.client.HTTPException, ValueError):
                # urllib wraps only some failures in URLError: a dropped connection surfaces as
                # ConnectionResetError, a truncated reply as HTTPException, and a non-JSON body
                # (a proxy error page, say) as JSONDecodeError. Retry them all, so callers only
                # ever see OpenRouterError: the planner's fail-open and the compactor catch
                # nothing else.
                pass
            else:
                return _content(body)
            if attempt < self._max_retries:
                self._sleep(2**attempt)
        raise OpenRouterError(f"OpenRouter still failing after {self._max_retries} retries")


def _content(body: Any) -> str:
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as err:
        raise OpenRouterError(f"malformed OpenRouter response: {err!r}") from err
    if not isinstance(content, str) or not content.strip():
        raise OpenRouterError("the model returned empty content")
    return content
