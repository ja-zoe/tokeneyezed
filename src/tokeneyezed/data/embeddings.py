"""The one embed() helper. Every collection embeds through here (master plan, "MongoDB data model").

One pinned Voyage model and output dimension, so vectors are comparable across collections. Calls
Voyage directly over HTTPS. Applies best-effort usage guards on the client side:

- a sliding-window limiter keeps requests/min and tokens/min under a fraction of the Tier 1 limits,
- a process token budget refuses new requests once reported usage exhausts the budget,
- 429 and 5xx responses are retried with exponential backoff and jitter (honoring Retry-After).

The write path uses embed_document_or_none(): if Voyage fails or the budget is spent, it returns
None so the caller writes the document without `embedding` and flags it for backfill. The agent
loop is never blocked on embeddings.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable, Sequence
from typing import Any

MODEL = "voyage-4"
DIMENSION = 1024  # must match numDimensions on the four Atlas vector indexes
API_URL = "https://api.voyageai.com/v1/embeddings"

MAX_BATCH = 128  # documents per request (Voyage maximum)
MAX_BATCH_TOKENS = 100_000  # stay under Voyage's per-request token cap
MAX_RETRIES = 5
CHARS_PER_TOKEN = 3  # heuristic, not an upper bound; actual usage can exceed reservations

# Tier 1 for voyage-4 is 8M TPM / 2000 RPM. Run at half of that by default.
DEFAULT_MAX_RPM = 1000
DEFAULT_MAX_TPM = 4_000_000
DEFAULT_TOKEN_BUDGET = 50_000_000  # total tokens per process; the free allowance is far larger

Transport = Callable[[dict[str, Any]], dict[str, Any]]


class EmbeddingError(Exception):
    """Voyage failed after retries, or returned something unusable."""


class BudgetExceeded(EmbeddingError):
    """The token budget for this process is spent. Not retryable."""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN + 1)


class RateLimiter:
    """Sliding 60-second window over request count and token count. Thread-safe."""

    def __init__(
        self,
        max_rpm: int,
        max_tpm: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.max_rpm = max_rpm
        self.max_tpm = max_tpm
        self._clock = clock
        self._sleep = sleep
        self._events: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    def acquire(self, tokens: int) -> None:
        """Block until one request of `tokens` fits in the window, then record it."""
        if tokens > self.max_tpm:
            raise ValueError(f"one request needs {tokens} tokens, over the {self.max_tpm} TPM cap")
        while True:
            with self._lock:
                now = self._clock()
                while self._events and now - self._events[0][0] >= 60:
                    self._events.popleft()
                used = sum(t for _, t in self._events)
                if len(self._events) < self.max_rpm and used + tokens <= self.max_tpm:
                    self._events.append((now, tokens))
                    return
                wait = 60 - (now - self._events[0][0])
            self._sleep(max(wait, 0.05))


class TokenBudget:
    """Admission budget tracking reservations and reported token usage.

    Estimates can be too low, so in-flight requests may exceed the limit. Settlement
    preserves the actual count and rejects an over-budget result; later calls are refused.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.spent = 0
        self._lock = threading.Lock()

    def reserve(self, tokens: int) -> None:
        with self._lock:
            if self.limit and self.spent + tokens > self.limit:
                raise BudgetExceeded(
                    f"token budget {self.limit} would be exceeded ({self.spent} spent)"
                )
            self.spent += tokens

    def settle(self, reserved: int, actual: int) -> None:
        """Replace the estimate with the real count Voyage reported.

        Already-billed usage cannot be undone. Preserve it even above the limit,
        then raise BudgetExceeded so the caller drops the result and later calls stop.
        """
        with self._lock:
            self.spent += actual - reserved
            if self.limit and self.spent > self.limit:
                raise BudgetExceeded(
                    f"Voyage used {actual} tokens, over the budget of {self.limit}"
                )


def _http_post(payload: dict[str, Any]) -> dict[str, Any]:
    key = os.environ.get("VOYAGE_API_KEY")
    if not key:
        raise EmbeddingError("VOYAGE_API_KEY is not set")
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


class Embedder:
    def __init__(
        self,
        transport: Transport = _http_post,
        limiter: RateLimiter | None = None,
        budget: TokenBudget | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport
        self._limiter = limiter or RateLimiter(
            _env_int("VOYAGE_MAX_RPM", DEFAULT_MAX_RPM), _env_int("VOYAGE_MAX_TPM", DEFAULT_MAX_TPM)
        )
        self._budget = budget or TokenBudget(_env_int("VOYAGE_TOKEN_BUDGET", DEFAULT_TOKEN_BUDGET))
        self._sleep = sleep
        self._query_cache: dict[str, list[float]] = {}

    def embed(self, texts: Sequence[str], input_type: str) -> list[list[float]]:
        """Embed texts in order. input_type is "document" when writing, "query" when searching."""
        if input_type not in ("document", "query"):
            raise ValueError(f"input_type must be 'document' or 'query', got {input_type!r}")
        vectors: list[list[float]] = []
        for batch in self._batches(list(texts)):
            vectors.extend(self._embed_batch(batch, input_type))
        return vectors

    def embed_query_cached(self, text: str) -> list[float]:
        """Embed a search query once and reuse it, e.g. an attempt's intent for every post-check."""
        if text not in self._query_cache:
            self._query_cache[text] = self.embed([text], "query")[0]
        return self._query_cache[text]

    def embed_document_or_none(self, text: str) -> list[float] | None:
        """Write-path helper: never raises. None means write without `embedding` and backfill."""
        try:
            return self.embed([text], "document")[0]
        except (EmbeddingError, ValueError, OSError):
            return None

    @staticmethod
    def _batches(texts: list[str]) -> list[list[str]]:
        batches: list[list[str]] = []
        current: list[str] = []
        current_tokens = 0
        for text in texts:
            tokens = estimate_tokens(text)
            if current and (
                len(current) >= MAX_BATCH or current_tokens + tokens > MAX_BATCH_TOKENS
            ):
                batches.append(current)
                current, current_tokens = [], 0
            current.append(text)
            current_tokens += tokens
        if current:
            batches.append(current)
        return batches

    def _embed_batch(self, batch: list[str], input_type: str) -> list[list[float]]:
        estimate = sum(estimate_tokens(t) for t in batch)
        self._budget.reserve(estimate)
        payload = {
            "input": batch,
            "model": MODEL,
            "input_type": input_type,
            "output_dimension": DIMENSION,
        }
        for attempt in range(MAX_RETRIES + 1):
            self._limiter.acquire(estimate)
            try:
                body = self._transport(payload)
            except urllib.error.HTTPError as err:
                if err.code != 429 and err.code < 500:
                    self._budget.settle(estimate, 0)
                    raise EmbeddingError(f"Voyage rejected the request: HTTP {err.code}") from err
                self._backoff(attempt, err.headers.get("Retry-After") if err.headers else None)
                continue
            except (urllib.error.URLError, TimeoutError):
                self._backoff(attempt, None)
                continue
            return self._parse(body, len(batch), estimate)
        self._budget.settle(estimate, 0)
        raise EmbeddingError(f"Voyage still failing after {MAX_RETRIES} retries")

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if attempt >= MAX_RETRIES:
            return
        delay = min(2**attempt, 30) + random.uniform(0, 0.5)
        if retry_after and retry_after.isdigit():
            delay = max(delay, int(retry_after))
        self._sleep(delay)

    def _parse(self, body: dict[str, Any], expected: int, estimate: int) -> list[list[float]]:
        if not isinstance(body, dict):
            raise EmbeddingError("malformed Voyage response: expected an object")
        usage = body.get("usage") or {}
        if not isinstance(usage, dict):
            raise EmbeddingError("malformed Voyage usage: expected an object")
        actual = usage.get("total_tokens")
        if actual is not None and (type(actual) is not int or actual < 0):
            raise EmbeddingError("malformed Voyage usage: expected a nonnegative token count")
        if actual is not None:
            self._budget.settle(estimate, actual)
        try:
            data = sorted(body["data"], key=lambda item: item["index"])
            vectors = [item["embedding"] for item in data]
        except (KeyError, TypeError) as err:
            raise EmbeddingError(f"malformed Voyage response: {err!r}") from err
        if len(vectors) != expected:
            raise EmbeddingError(f"asked for {expected} embeddings, got {len(vectors)}")
        for vector in vectors:
            if not isinstance(vector, list) or len(vector) != DIMENSION:
                raise EmbeddingError(f"expected {DIMENSION} dimensions")
        return vectors


_default: Embedder | None = None


def get_embedder() -> Embedder:
    global _default
    if _default is None:
        _default = Embedder()
    return _default


def embed(texts: Sequence[str], input_type: str) -> list[list[float]]:
    return get_embedder().embed(texts, input_type)
