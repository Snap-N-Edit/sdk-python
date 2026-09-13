"""Unit tests: error mapping, retry policy and timeouts."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from _fake import FakeApi, create_body, job_body, upload_routes
from snapnedit import (
    AsyncSnapnedit,
    ErrorCode,
    RetryPolicy,
    Snapnedit,
    SnapneditError,
    SnapneditTimeoutError,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"payload"


def client(api: FakeApi, **kwargs: object) -> Snapnedit:
    return Snapnedit("sk_test", "http://api.test", transport=api.transport, **kwargs)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries and polls run instantly."""
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    monkeypatch.setattr("snapnedit.client.time.sleep", lambda _seconds: None)

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("snapnedit.async_client.asyncio.sleep", _instant)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, ErrorCode.INVALID_INPUT),
        (401, ErrorCode.UNAUTHORIZED),
        (402, ErrorCode.PAYMENT_REQUIRED),
        (403, ErrorCode.FORBIDDEN),
        (404, ErrorCode.NOT_FOUND),
    ],
)
def test_error_envelope_becomes_a_typed_error(status: int, code: ErrorCode):
    api = FakeApi().on(
        "GET", "/jobs/j", status=status, json_body={"error": {"code": code.value, "message": "no"}}
    )
    with client(api) as snap, pytest.raises(SnapneditError) as caught:
        snap.get_job("j")
    assert caught.value.code is code
    assert caught.value.code == code.value
    assert caught.value.status == status
    assert caught.value.message == "no"


def test_an_unknown_error_code_degrades_to_internal():
    api = FakeApi().on(
        "GET", "/jobs/j", status=418, json_body={"error": {"code": "brand_new", "message": "hm"}}
    )
    with client(api) as snap, pytest.raises(SnapneditError) as caught:
        snap.get_job("j")
    assert caught.value.code is ErrorCode.INTERNAL
    assert caught.value.status == 418


def test_a_non_json_failure_still_raises_with_the_status():
    api = FakeApi().on("GET", "/jobs/j", status=502, content=b"<html>bad gateway</html>")
    with client(api, max_retries=0) as snap, pytest.raises(SnapneditError) as caught:
        snap.get_job("j")
    assert caught.value.code is ErrorCode.INTERNAL
    assert caught.value.status == 502
    assert "502" in caught.value.message


def test_a_non_json_success_body_is_an_internal_error():
    api = FakeApi().on("GET", "/jobs/j", status=200, content=b"not json")
    with client(api) as snap, pytest.raises(SnapneditError) as caught:
        snap.get_job("j")
    assert caught.value.code is ErrorCode.INTERNAL


def test_a_failed_job_raises_from_run_with_its_own_code():
    api = FakeApi()
    upload_routes(api)
    api.on("POST", "/jobs", status=202, json_body=create_body())
    api.on(
        "GET",
        "/jobs/job-1",
        json_body=job_body(
            state="failed",
            errorCode="input_fetch_failed",
            message="could not fetch",
            outputAssetId=None,
            download=None,
        ),
    )
    with client(api) as snap, pytest.raises(SnapneditError) as caught:
        snap.run("remove-background", PNG, poll_interval=0.0)
    assert caught.value.code is ErrorCode.INPUT_FETCH_FAILED
    assert caught.value.status == 200


def test_polling_gives_up_with_a_timeout_error():
    api = FakeApi()
    upload_routes(api)
    api.on("POST", "/jobs", status=202, json_body=create_body())
    api.on("GET", "/jobs/job-1", json_body=job_body(state="queued"))
    with client(api) as snap, pytest.raises(SnapneditTimeoutError) as caught:
        snap.run("remove-background", PNG, poll_interval=0.0, timeout=0.0)
    assert "job-1" in caught.value.message
    assert isinstance(caught.value, SnapneditError)


def test_a_get_is_retried_through_429_and_5xx():
    api = FakeApi().sequence(
        "GET",
        "/jobs/j",
        [
            httpx.Response(429, json={"error": {"code": "rate_limited", "message": "slow down"}},
                           headers={"retry-after": "0"}),
            httpx.Response(503, json={"error": {"code": "internal", "message": "later"}}),
            httpx.Response(200, json=job_body()),
        ],
    )
    with client(api, max_retries=2) as snap:
        assert snap.get_job("j").state == "succeeded"
    assert len(api.sent("GET", "/jobs/j")) == 3


def test_retries_are_bounded_and_the_last_failure_is_raised():
    api = FakeApi().on(
        "GET", "/jobs/j", status=429, json_body={"error": {"code": "rate_limited", "message": "no"}}
    )
    with client(api, max_retries=2) as snap, pytest.raises(SnapneditError) as caught:
        snap.get_job("j")
    assert caught.value.code is ErrorCode.RATE_LIMITED
    assert len(api.sent("GET", "/jobs/j")) == 3


def test_a_post_is_never_retried():
    api = FakeApi().on(
        "POST", "/jobs", status=503, json_body={"error": {"code": "internal", "message": "no"}}
    )
    with client(api, max_retries=3) as snap, pytest.raises(SnapneditError):
        snap.create_job("remove-background", "a")
    assert len(api.sent("POST", "/jobs")) == 1


def test_a_network_error_is_retried_then_reported():
    calls = {"n": 0}

    def flaky(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("connection refused")

    snap = Snapnedit("sk", "http://api.test", transport=httpx.MockTransport(flaky), max_retries=1)
    with snap, pytest.raises(SnapneditError) as caught:
        snap.get_job("j")
    assert calls["n"] == 2
    assert caught.value.status == 0
    assert "network error" in caught.value.message


def test_retry_policy_backs_off_within_bounds_and_honors_retry_after():
    policy = RetryPolicy(max_retries=3, backoff_base=0.5, backoff_max=8.0)
    assert policy.should_retry_status(429) is True
    assert policy.should_retry_status(503) is True
    assert policy.should_retry_status(404) is False
    assert 0.25 <= policy.delay(1) <= 0.5
    assert 0.5 <= policy.delay(2) <= 1.0
    assert policy.delay(50) <= 8.0
    assert policy.delay(1, retry_after=2.0) == 2.0
    assert policy.delay(1, retry_after=1_000.0) == 8.0


def test_download_result_refuses_when_there_is_no_copy_left():
    api = FakeApi().on(
        "GET",
        "/jobs/j",
        json_body=job_body(
            download=None,
            destination={"type": "saved", "id": "d"},
            delivery={"status": "delivered", "attempts": 1, "localCopyDeleted": True},
        ),
    )
    with client(api) as snap:
        view = snap.get_job("j")
        with pytest.raises(SnapneditError) as caught:
            snap.download_result(view)
    assert caught.value.code is ErrorCode.NOT_FOUND


def test_the_async_client_maps_errors_the_same_way():
    api = FakeApi().on(
        "GET", "/jobs/j", status=402,
        json_body={"error": {"code": "payment_required", "message": "out of credits"}},
    )

    async def main() -> None:
        async with AsyncSnapnedit(
            "sk", "http://api.test", transport=api.async_transport
        ) as snap:
            with pytest.raises(SnapneditError) as caught:
                await snap.get_job("j")
            assert caught.value.code is ErrorCode.PAYMENT_REQUIRED
            assert caught.value.status == 402

    asyncio.run(main())


def test_the_async_client_retries_the_same_way():
    api = FakeApi().sequence(
        "GET",
        "/jobs/j",
        [
            httpx.Response(500, json={"error": {"code": "internal", "message": "boom"}}),
            httpx.Response(200, json=job_body()),
        ],
    )

    async def main() -> None:
        async with AsyncSnapnedit(
            "sk", "http://api.test", transport=api.async_transport, max_retries=1
        ) as snap:
            assert (await snap.get_job("j")).state == "succeeded"

    asyncio.run(main())
    assert len(api.sent("GET", "/jobs/j")) == 2
