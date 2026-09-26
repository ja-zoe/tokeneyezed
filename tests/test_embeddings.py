"""Limit guards for the embed() helper. No network: Voyage is replaced by a fake transport."""

import urllib.error
from typing import Any

import pytest

from tokeneyezed.data.embeddings import (
    DIMENSION,
    BudgetExceeded,
    Embedder,
    EmbeddingError,
    RateLimiter,
    TokenBudget,
)


def vector() -> list[float]:
    return [0.1] * DIMENSION


def ok_body(count: int, tokens: int = 5) -> dict[str, Any]:
    return {
        "data": [{"index": i, "embedding": vector()} for i in range(count)],
        "usage": {"total_tokens": tokens},
    }


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "err", {}, None)  # type: ignore[arg-type]


def make(transport, *, max_rpm=1000, max_tpm=4_000_000, budget=50_000_000, clock=None, sleeps=None):
    sleeps = sleeps if sleeps is not None else []
    now = clock or (lambda: 0.0)
    return Embedder(
        transport=transport,
        limiter=RateLimiter(max_rpm, max_tpm, clock=now, sleep=sleeps.append),
        budget=TokenBudget(budget),
        sleep=sleeps.append,
    )


def test_rate_limiter_waits_when_request_count_is_full() -> None:
    sleeps: list[float] = []
    t = [0.0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        t[0] += seconds

    limiter = RateLimiter(2, 1_000_000, clock=lambda: t[0], sleep=sleep)
    limiter.acquire(10)
    limiter.acquire(10)
    limiter.acquire(10)  # third request in the same minute must wait for the window to clear
    assert sleeps and sum(sleeps) >= 60


def test_rate_limiter_waits_when_token_count_is_full() -> None:
    sleeps: list[float] = []
    t = [0.0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        t[0] += seconds

    limiter = RateLimiter(1000, 100, clock=lambda: t[0], sleep=sleep)
    limiter.acquire(80)
    limiter.acquire(80)
    assert sum(sleeps) >= 60


def test_rate_limiter_rejects_a_request_that_can_never_fit() -> None:
    with pytest.raises(ValueError):
        RateLimiter(10, 100).acquire(101)


def test_budget_stops_before_calling_voyage() -> None:
    calls: list[Any] = []
    embedder = make(lambda p: calls.append(p) or ok_body(1), budget=3)
    with pytest.raises(BudgetExceeded):
        embedder.embed(
            ["a fairly long piece of text that costs more than three tokens"], "document"
        )
    assert calls == []


def test_budget_is_settled_to_the_real_token_count() -> None:
    embedder = make(lambda p: ok_body(1, tokens=2), budget=1000)
    embedder.embed(["hello world"], "document")
    assert embedder._budget.spent == 2


def test_retries_429_with_backoff_then_succeeds() -> None:
    responses = [http_error(429), http_error(429), ok_body(1)]
    sleeps: list[float] = []

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    embedder = make(transport, sleeps=sleeps)
    assert len(embedder.embed(["x"], "document")) == 1
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]


def test_gives_up_after_max_retries() -> None:
    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        raise http_error(429)

    with pytest.raises(EmbeddingError):
        make(transport).embed(["x"], "document")


def test_client_errors_are_not_retried() -> None:
    calls = []

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        raise http_error(401)

    with pytest.raises(EmbeddingError):
        make(transport).embed(["x"], "document")
    assert len(calls) == 1


def test_wrong_dimension_is_rejected() -> None:
    body = {"data": [{"index": 0, "embedding": [0.1] * 10}], "usage": {"total_tokens": 1}}
    with pytest.raises(EmbeddingError):
        make(lambda p: body).embed(["x"], "document")


def test_write_path_returns_none_instead_of_raising() -> None:
    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        raise http_error(500)

    assert make(transport).embed_document_or_none("x") is None
    assert make(lambda p: ok_body(1), budget=1).embed_document_or_none("x" * 500) is None


def test_query_embedding_is_cached() -> None:
    calls = []

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return ok_body(1)

    embedder = make(transport)
    embedder.embed_query_cached("fix emphasis")
    embedder.embed_query_cached("fix emphasis")
    assert len(calls) == 1 and calls[0]["input_type"] == "query"


def test_payload_pins_model_dimension_and_input_type() -> None:
    seen: list[dict[str, Any]] = []
    embedder = make(lambda p: seen.append(p) or ok_body(1))
    embedder.embed(["x"], "document")
    assert seen[0]["model"] == "voyage-4"
    assert seen[0]["output_dimension"] == DIMENSION
    assert seen[0]["input_type"] == "document"


def test_large_input_is_split_into_batches_of_at_most_128() -> None:
    sizes: list[int] = []

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        sizes.append(len(payload["input"]))
        return ok_body(len(payload["input"]))

    result = make(transport).embed(["x"] * 300, "document")
    assert len(result) == 300 and sizes == [128, 128, 44]


def test_invalid_input_type_is_rejected() -> None:
    with pytest.raises(ValueError):
        make(lambda p: ok_body(1)).embed(["x"], "doc")
