"""Every scenario in the monorepo's `test/conformance/scenarios.json`, in Python.

One test per scenario id, parametrized on the id itself so the pytest node id
carries it (`test_jobs_cache_hit[jobs.cache-hit]`) and `grep` finds the test
when a scenario changes. The last test in the file fails if `scenarios.json`
contains an id this file does not implement.

These run against the REAL api and worker — see `conftest.py`. Everything is
asserted through the public SDK surface wherever the SDK can express it, and
through raw HTTP where the scenario pins something the SDK deliberately hides
(an exact status code, the absence of a secret in a response body).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any, TypeVar

import pytest

from conftest import Conformance
from snapnedit import (
    ERROR_CODES,
    AsyncSnapnedit,
    ErrorCode,
    PresignedPutDestination,
    SavedDestination,
    SnapneditError,
    UrlInput,
    Webhooks,
)

IMPLEMENTED: set[str] = set()
F = TypeVar("F", bound=Callable[..., Any])

POLL = {"poll_interval": 0.05, "timeout": 60.0}


def scenario(scenario_id: str) -> Callable[[F], F]:
    """Mark a test as the implementation of one scenario id."""
    IMPLEMENTED.add(scenario_id)

    def decorate(fn: F) -> F:
        marked = pytest.mark.parametrize("scenario_id", [scenario_id])(fn)
        return pytest.mark.conformance(marked)  # type: ignore[return-value]

    return decorate


def auth(conf: Conformance) -> dict[str, str]:
    """Build the seeded account's bearer header, for raw HTTP assertions."""
    return {"authorization": f"Bearer {conf.api_key}"}


def in_the_future(timestamp: str) -> bool:
    """Return whether an ISO-8601 timestamp is still ahead of us."""
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return parsed > datetime.now(timezone.utc)


def instant(timestamp: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerating the `Z` suffix."""
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def in_the_past_or_now(timestamp: str) -> bool:
    """Return whether an ISO-8601 timestamp has already happened."""
    return instant(timestamp) <= datetime.now(timezone.utc)


# ---------------------------------------------------------------- auth ----


@scenario("auth.missing-credential")
def test_auth_missing_credential(conformance: Conformance, scenario_id: str):
    with pytest.raises(SnapneditError) as caught:
        conformance.client(None).destinations.list()
    assert caught.value.status == 401
    assert caught.value.code is ErrorCode.UNAUTHORIZED

    raw = conformance.raw.get("/destinations")
    assert raw.status_code == 401
    assert list(raw.json().keys()) == ["error"]
    assert raw.json()["error"]["code"] == "unauthorized"


@scenario("auth.bad-key")
def test_auth_bad_key(conformance: Conformance, scenario_id: str):
    with pytest.raises(SnapneditError) as caught:
        conformance.client("sk_live_this_key_does_not_exist").destinations.list()
    assert caught.value.status == 401
    assert caught.value.code is ErrorCode.UNAUTHORIZED
    assert conformance.account_id not in caught.value.message


@scenario("auth.publishable-key-is-not-a-bearer")
def test_auth_publishable_key_is_not_a_bearer(conformance: Conformance, scenario_id: str):
    with pytest.raises(SnapneditError) as caught:
        conformance.client(conformance.publishable_key).destinations.list()
    assert caught.value.status == 401
    assert caught.value.code is ErrorCode.UNAUTHORIZED


# ---------------------------------------------------------- operations ----


@scenario("operations.list")
def test_operations_list(conformance: Conformance, scenario_id: str):
    operations = conformance.client(None).list_operations()
    assert len(operations) == 17
    assert [op.id for op in operations] == [
        "remove-background",
        "upscale",
        "unblur",
        "colorize",
        "style-transfer",
        "retouch",
        "beautify",
        "magic-eraser",
        "generative-fill",
        "remove-watermark",
        "ai-denoise",
        "replace-sky",
        "relight",
        "replace-background",
        "strip-metadata",
        "auto-remove-watermark",
        "resize-image",
    ]
    for op in operations:
        assert op.label and op.description and op.seo_title
        assert op.accept
        assert op.max_input_dimension > 0
        assert isinstance(op.params_json_schema, dict)
    assert sorted(op.id for op in operations if op.requires_mask) == [
        "generative-fill",
        "magic-eraser",
        "remove-watermark",
    ]

    upscale = next(op for op in operations if op.id == "upscale")
    factor = upscale.params_json_schema["properties"]["factor"]
    assert factor["enum"] == ["2", "4"]
    assert factor["default"] == "2"

    spec = conformance.raw.get("/openapi.json").json()
    costs = spec["paths"]["/operations"]["get"]["x-credit-cost"]
    assert costs["resize-image"] == 0
    assert costs["remove-background"] == 1
    assert costs["upscale"] == 2
    assert spec["components"]["schemas"]["OperationParams.resize-image"]["x-credit-cost"] == 0

    # The SDK's static catalog must agree with the live one.
    from snapnedit import CREDIT_COSTS, OPERATION_IDS

    assert list(OPERATION_IDS) == [op.id for op in operations]
    assert costs == CREDIT_COSTS


# --------------------------------------------------------- the core flow --


@scenario("jobs.happy-path")
def test_jobs_happy_path(conformance: Conformance, scenario_id: str):
    data = conformance.unique_image("happy")

    created = conformance.raw.post(
        "/uploads", json={"mime": "image/png", "bytes": len(data)}, headers=auth(conformance)
    )
    assert created.status_code == 200, created.text
    asset_id = created.json()["assetId"]
    upload_url = created.json()["upload"]["url"]
    assert created.json()["upload"]["expiresAt"]

    put = conformance.raw.put(upload_url, content=data, headers={"content-type": "image/png"})
    assert put.status_code == 204

    confirmed = conformance.raw.post(
        f"/uploads/{asset_id}/confirm", headers=auth(conformance)
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["assetId"] == asset_id
    assert re.fullmatch(r"[0-9a-f]{64}", confirmed.json()["contentHash"])
    assert confirmed.json()["bytes"] == len(data)

    job = conformance.raw.post(
        "/jobs",
        json={"operation": "remove-background", "inputAssetId": asset_id},
        headers=auth(conformance),
    )
    assert job.status_code == 202, job.text
    assert job.json()["status"] == {"state": "queued"}
    assert job.json()["input"] == {"kind": "asset"}
    assert job.json()["destination"] is None
    assert job.json()["delivery"] is None

    snap = conformance.client()
    view = snap.wait_for_job(job.json()["jobId"], **POLL)
    assert view.state == "succeeded"
    assert view.output_asset_id
    assert view.download is not None and view.download.expires_at

    downloaded = conformance.raw.get(view.download.url)
    assert downloaded.status_code == 200
    assert len(downloaded.content) > 0
    assert "image/png" in downloaded.headers["content-type"]

    # The same flow through the SDK, end to end.
    result = snap.run("remove-background", conformance.unique_image("happy-sdk"), **POLL)
    assert result.downloaded is True
    assert result.output and result.mime == "image/png"
    assert result.input.kind == "asset"


@scenario("jobs.params-validation")
def test_jobs_params_validation(conformance: Conformance, scenario_id: str):
    snap = conformance.client()
    asset = snap.upload(conformance.unique_image("params")).asset_id

    before = conformance.balance()
    with pytest.raises(SnapneditError) as caught:
        snap.create_job("upscale", asset, {"factor": "3"})
    assert caught.value.status == 400
    assert caught.value.code is ErrorCode.INVALID_INPUT
    assert conformance.balance() == before

    for factor in ("2", "4"):
        created = snap.create_job("upscale", asset, {"factor": factor})
        assert created.http_status == 202


@scenario("jobs.unknown-param-rejected")
def test_jobs_unknown_param_rejected(conformance: Conformance, scenario_id: str):
    snap = conformance.client()
    asset = snap.upload(conformance.unique_image("strict-params")).asset_id
    with pytest.raises(SnapneditError) as caught:
        snap.create_job("upscale", asset, {"factor": "2", "nope": "x"})
    assert caught.value.status == 400
    assert caught.value.code is ErrorCode.INVALID_INPUT


# -------------------------------------------------------------- credits --


@scenario("jobs.free-operation")
def test_jobs_free_operation(conformance: Conformance, scenario_id: str):
    snap = conformance.client()
    before = conformance.balance()
    result = snap.run("resize-image", conformance.unique_image("free-op"), {"width": 4}, **POLL)
    assert result.downloaded is True
    assert conformance.balance() == before


@scenario("jobs.credit-debit")
def test_jobs_credit_debit(conformance: Conformance, scenario_id: str):
    snap = conformance.client()
    before = conformance.balance()
    asset = snap.upload(conformance.unique_image("debit")).asset_id
    created = snap.create_job("remove-background", asset)
    assert created.http_status == 202
    assert created.cached is False
    assert created.credit_cost == 1
    # The debit lands at creation time, before the worker ever sees the job.
    assert conformance.balance() == before - 1
    assert snap.wait_for_job(created.job_id, **POLL).state == "succeeded"
    assert conformance.balance() == before - 1


@scenario("jobs.cache-hit")
def test_jobs_cache_hit(conformance: Conformance, scenario_id: str):
    snap = conformance.client()
    asset = snap.upload(conformance.unique_image("cache")).asset_id
    first = snap.create_job("remove-background", asset)
    done = snap.wait_for_job(first.job_id, **POLL)
    assert done.state == "succeeded"

    before = conformance.balance()
    second = snap.create_job("remove-background", asset)
    assert second.http_status == 200
    assert second.cached is True
    assert second.credit_cost == 0
    assert second.state == "succeeded"
    # A cache hit records its own job row, so the id is NEW and only the
    # result is shared. Never assert the two ids match.
    assert second.job_id != first.job_id
    assert second.status.output_asset_id == done.output_asset_id
    assert conformance.balance() == before


# --------------------------------------------------- bring your own storage


@scenario("jobs.url-input")
def test_jobs_url_input(conformance: Conformance, scenario_id: str):
    nonce = conformance.nonce("url-input")
    url = f"{conformance.url}/__conformance/fixtures/small.png?nonce={nonce}"

    with pytest.raises(SnapneditError) as caught:
        conformance.client(None).create_job("remove-background", UrlInput(url))
    assert caught.value.status == 403
    assert caught.value.code is ErrorCode.FORBIDDEN

    snap = conformance.client()
    created = snap.create_job("remove-background", UrlInput(url))
    assert created.http_status == 202
    assert created.input.kind == "url"

    view = snap.wait_for_job(created.job_id, **POLL)
    assert view.state == "succeeded", view.message
    assert view.input.kind == "url"
    assert view.download is not None
    assert len(snap.download_result(view)) > 0

    # A url can be a bearer credential for someone's bucket: it is never echoed.
    echoed = conformance.raw.get(f"/jobs/{created.job_id}", headers=auth(conformance))
    assert "nonce=" not in echoed.text
    assert nonce not in echoed.text


@scenario("jobs.url-input-fetch-failed")
def test_jobs_url_input_fetch_failed(conformance: Conformance, scenario_id: str):
    snap = conformance.client()
    before = conformance.balance()
    created = snap.create_job(
        "remove-background",
        UrlInput(f"{conformance.url}/__conformance/fixtures/does-not-exist.png"),
    )
    assert created.http_status == 202  # the worker fetches it, so creation succeeds

    view = snap.wait_for_job(created.job_id, **POLL)
    assert view.state == "failed"
    assert view.error_code is ErrorCode.INPUT_FETCH_FAILED
    assert conformance.balance() == before  # a terminal failure is refunded

    # run() surfaces the same failure as a typed error.
    with pytest.raises(SnapneditError) as caught:
        snap.run(
            "remove-background",
            UrlInput(f"{conformance.url}/__conformance/fixtures/nope.png"),
            **POLL,
        )
    assert caught.value.code is ErrorCode.INPUT_FETCH_FAILED


@scenario("jobs.presigned-destination")
def test_jobs_presigned_destination(conformance: Conformance, scenario_id: str):
    key = f"presigned/{conformance.nonce('put')}.png"
    snap = conformance.client()
    result = snap.run(
        "remove-background",
        conformance.unique_image("presigned-dest"),
        destination=PresignedPutDestination(
            url=f"{conformance.url}/__conformance/bucket/{key}",
            headers={"content-type": "image/png"},
        ),
        **POLL,
    )

    assert result.destination is not None
    assert result.destination.type == "presigned-put"
    assert result.destination.id is None  # never the url, never the headers
    assert result.delivery is not None
    assert result.delivery.status == "delivered"
    assert result.delivery.attempts >= 1
    assert result.delivery.delivered_at
    assert result.download is not None  # a presigned-put delivery keeps our copy
    assert result.downloaded is False  # …but run() does not pull the bytes back

    echoed = conformance.raw.get(f"/jobs/{result.job_id}", headers=auth(conformance))
    assert "__conformance/bucket" not in echoed.text

    stored = conformance.bucket_object(key)
    assert stored is not None
    assert stored["bytes"] > 0
    assert stored["contentType"] == "image/png"


# -------------------------------------------------- saved destinations ----


@scenario("destinations.crud")
def test_destinations_crud(conformance: Conformance, scenario_id: str):
    snap = conformance.client()
    body = conformance.destination_body(key_prefix="crud/")
    dest = snap.destinations.create(**body)

    assert dest.id
    assert dest.name == body["name"]
    assert dest.provider == "s3-compatible"
    assert dest.bucket == conformance.bucket
    assert dest.region == "auto"
    assert dest.endpoint == conformance.url
    assert dest.force_path_style is True
    assert dest.key_prefix == "crud/"
    assert dest.access_key_id_last4 == body["access_key_id"][-4:]
    assert dest.is_default is False
    assert dest.delete_after_delivery is False
    assert dest.last_test is None
    assert dest.created_at and dest.updated_at

    listed = snap.destinations.list()
    assert dest.id in [row.id for row in listed]
    raw_list = conformance.raw.get("/destinations", headers=auth(conformance))
    assert body["secret_access_key"] not in raw_list.text

    patched = snap.destinations.update(dest.id, name="renamed by conformance")
    assert patched.name == "renamed by conformance"

    probe = snap.destinations.test(dest.id)
    assert probe.ok is True
    assert probe.error is None
    assert probe.latency_ms >= 0

    assert snap.destinations.delete(dest.id) is None
    deleted = conformance.raw.delete(f"/destinations/{dest.id}", headers=auth(conformance))
    assert deleted.status_code == 404

    for call in (
        lambda: snap.destinations.update(dest.id, name="nope"),
        lambda: snap.destinations.test(dest.id),
        lambda: snap.destinations.delete(dest.id),
    ):
        with pytest.raises(SnapneditError) as caught:
            call()
        assert caught.value.status == 404
        assert caught.value.code is ErrorCode.NOT_FOUND

    # An id on another account is 404 too, never 403.
    with pytest.raises(SnapneditError) as caught:
        snap.destinations.update("00000000-0000-4000-8000-000000000000", name="nope")
    assert caught.value.status == 404


@scenario("destinations.presign")
def test_destinations_presign(conformance: Conformance, scenario_id: str):
    snap = conformance.client()
    dest = snap.destinations.create(**conformance.destination_body(key_prefix="exports/"))
    try:
        with pytest.raises(SnapneditError) as caught:
            snap.destinations.presign_upload(dest.id, ext="png", content_type="image/jpeg")
        assert caught.value.status == 400
        assert caught.value.code is ErrorCode.INVALID_INPUT

        signed = snap.destinations.presign_upload(dest.id, ext="png", content_type="image/png")
        assert signed.method == "PUT"
        assert signed.bucket == conformance.bucket
        assert re.fullmatch(
            r"exports/\d{4}/\d{2}/\d{2}/export-\d+-[a-z0-9]{6}\.png", signed.key
        ), signed.key
        assert signed.headers["content-type"] == "image/png"
        assert in_the_future(signed.expires_at)

        payload = b"conformance export bytes"
        put = conformance.raw.put(signed.url, content=payload, headers=signed.headers)
        assert put.status_code == 200, put.text
        stored = conformance.bucket_object(signed.key)
        assert stored is not None and stored["bytes"] == len(payload)
    finally:
        snap.destinations.delete(dest.id)


@scenario("jobs.saved-destination")
def test_jobs_saved_destination(conformance: Conformance, scenario_id: str):
    snap = conformance.client()

    with pytest.raises(SnapneditError) as caught:
        snap.create_job(
            "remove-background",
            snap.upload(conformance.unique_image("saved-foreign")).asset_id,
            destination=SavedDestination("00000000-0000-4000-8000-000000000000"),
        )
    assert caught.value.status == 404
    assert caught.value.code is ErrorCode.NOT_FOUND

    dest = snap.destinations.create(**conformance.destination_body(key_prefix="saved/"))
    try:
        result = snap.run(
            "remove-background",
            conformance.unique_image("saved-dest"),
            destination=SavedDestination(dest.id),
            **POLL,
        )
        assert result.destination is not None
        assert result.destination.type == "saved"
        assert result.destination.id == dest.id
        assert result.delivery is not None
        assert result.delivery.status == "delivered"
        assert result.delivery.bucket == conformance.bucket
        assert result.delivery.key is not None
        assert re.fullmatch(
            rf"saved/\d{{4}}/\d{{2}}/\d{{2}}/{re.escape(result.job_id)}\.png",
            result.delivery.key,
        ), result.delivery.key

        stored = conformance.bucket_object(result.delivery.key)
        assert stored is not None and stored["bytes"] > 0
    finally:
        snap.destinations.delete(dest.id)


@scenario("jobs.default-destination-and-opt-out")
def test_jobs_default_destination_and_opt_out(conformance: Conformance, scenario_id: str):
    snap = conformance.client()
    dest = snap.destinations.create(
        **conformance.destination_body(key_prefix="default/", is_default=True)
    )
    try:
        inherited = snap.run("remove-background", conformance.unique_image("default-on"), **POLL)
        assert inherited.destination is not None
        assert inherited.destination.type == "saved"
        assert inherited.destination.id == dest.id
        assert inherited.delivery is not None
        assert inherited.delivery.status == "delivered"
        assert (inherited.delivery.key or "").startswith("default/")
        # An account default does not stop run() from handing back the bytes.
        assert inherited.downloaded is True

        opted_out = snap.run(
            "remove-background",
            conformance.unique_image("default-off"),
            destination=None,
            **POLL,
        )
        assert opted_out.destination is None
        assert opted_out.delivery is None
        assert opted_out.downloaded is True
    finally:
        snap.destinations.delete(dest.id)


# ------------------------------------------- masks, errors, ownership ----


@scenario("jobs.mask-required")
def test_jobs_mask_required(conformance: Conformance, scenario_id: str):
    snap = conformance.client()
    asset = snap.upload(conformance.unique_image("mask-missing")).asset_id

    with pytest.raises(SnapneditError) as caught:
        snap.create_job("magic-eraser", asset)
    assert caught.value.status == 400
    assert caught.value.code is ErrorCode.INVALID_INPUT
    assert "maskAssetId" in caught.value.message

    # The catalog is how an SDK knows to demand a mask up front.
    catalog = {op.id: op.requires_mask for op in snap.list_operations()}
    assert catalog["magic-eraser"] is True
    from snapnedit import requires_mask

    assert requires_mask("magic-eraser") is True

    mask = snap.upload(conformance.unique_image("mask-bytes")).asset_id
    created = snap.create_job("magic-eraser", asset, {"maskAssetId": mask})
    assert created.http_status == 202


@scenario("jobs.foreign-job-is-404")
def test_jobs_foreign_job_is_404(conformance: Conformance, scenario_id: str):
    snap = conformance.client()
    asset = snap.upload(conformance.unique_image("ownership")).asset_id
    created = snap.create_job("remove-background", asset)

    assert snap.get_job(created.job_id).job_id == created.job_id

    anonymous = conformance.raw.get(f"/jobs/{created.job_id}")
    assert anonymous.status_code == 404
    assert anonymous.json()["error"]["code"] == "not_found"

    wrong_key = conformance.raw.get(
        f"/jobs/{created.job_id}", headers={"authorization": "Bearer sk_live_some_other_account"}
    )
    assert wrong_key.status_code == 404

    never_existed = conformance.raw.get("/jobs/00000000-0000-4000-8000-000000000000")
    assert never_existed.status_code == 404
    # A job id must not be an existence oracle: same code, same shape — the only
    # thing that differs between "not yours" and "never existed" is the echoed id.
    assert never_existed.json()["error"]["code"] == anonymous.json()["error"]["code"]
    assert never_existed.json()["error"]["message"].replace(
        "00000000-0000-4000-8000-000000000000", ""
    ) == anonymous.json()["error"]["message"].replace(created.job_id, "")

    with pytest.raises(SnapneditError) as caught:
        conformance.client(None).get_job(created.job_id)
    assert caught.value.code is ErrorCode.NOT_FOUND


# ----------------------------------------------------------------- usage --


@scenario("usage.query")
def test_usage_query(conformance: Conformance, scenario_id: str):
    snap = conformance.client()

    # One paid job and one free one, on bytes nobody has submitted before: a
    # cache hit would be free and would not move `credits`.
    paid = snap.run("upscale", conformance.unique_image("usage-paid"), **POLL)
    assert paid.job_id
    free = snap.run("resize-image", conformance.unique_image("usage-free"), {"width": 4}, **POLL)
    assert free.job_id

    report = snap.get_usage(group_by="operation")
    assert report.group_by == "operation"
    assert in_the_past_or_now(report.range.start)
    assert instant(report.range.end) >= instant(report.range.start)

    # Every scenario shares one seeded account, so these are lower bounds.
    assert report.totals.jobs >= 2
    assert report.totals.credits >= 2
    assert report.totals.free >= 1
    for name in ("cache_hits", "failed", "delivered", "delivery_failed", "sessions",
                 "active_sessions"):
        value = getattr(report.totals, name)
        assert isinstance(value, int) and value >= 0, name

    upscale = report.point("upscale")
    resize = report.point("resize-image")
    assert upscale is not None, "no upscale bucket"
    assert resize is not None, "no resize-image bucket"
    # A per-operation bucket's credits is jobs x cost, so with N upscales the
    # honest assertion is "a multiple of 2, at least 2".
    assert upscale.credits >= 2
    assert upscale.credits % 2 == 0
    assert resize.credits == 0
    for entry in report.series:
        assert entry.key and isinstance(entry.label, str)
        for name in ("jobs", "credits", "cache_hits", "free", "failed", "delivered",
                     "delivery_failed", "sessions"):
            assert isinstance(getattr(entry, name), int), f"{entry.key}.{name}"

    # The cap gauge — an `sk_` caller sees every live key on the account.
    assert len(report.keys) >= 2
    for key in report.keys:
        assert key.id and key.name
        assert key.kind in ("secret", "publishable")
        assert key.daily_credit_limit is None or isinstance(key.daily_credit_limit, int)
        assert key.used_today >= 0

    by_day = snap.get_usage(group_by="day")
    assert by_day.group_by == "day"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert by_day.point(today) is not None

    with pytest.raises(SnapneditError) as unauthorized:
        conformance.client(None).get_usage()
    assert unauthorized.value.status == 401
    assert unauthorized.value.code is ErrorCode.UNAUTHORIZED

    with pytest.raises(SnapneditError) as backwards:
        snap.get_usage(start=date(2026, 9, 13), end=date(2026, 9, 1))
    assert backwards.value.status == 400
    assert backwards.value.code is ErrorCode.INVALID_INPUT

    # The query really is the wire's `from`/`to`, spelled out over raw HTTP.
    raw = conformance.raw.get(
        "/usage", params={"from": "2026-09-13", "to": "2026-09-01"}, headers=auth(conformance)
    )
    assert raw.status_code == 400
    assert raw.json()["error"]["code"] == "invalid_input"


@scenario("errors.envelope")
def test_errors_envelope(conformance: Conformance, scenario_id: str):
    responses = [
        conformance.raw.post("/jobs", json={}, headers=auth(conformance)),
        conformance.raw.get(
            "/jobs/00000000-0000-4000-8000-000000000000", headers=auth(conformance)
        ),
        conformance.raw.get("/destinations"),
        conformance.raw.post(
            "/uploads", json={"mime": "application/pdf", "bytes": 10}, headers=auth(conformance)
        ),
    ]
    assert [r.status_code for r in responses] == [400, 404, 401, 400]

    known = {code.value for code in ERROR_CODES}
    assert known == {
        "invalid_input",
        "unsupported_mime",
        "too_large",
        "not_found",
        "input_fetch_failed",
        "provider_failed",
        "provider_exhausted",
        "rate_limited",
        "bot_check_failed",
        "unauthorized",
        "forbidden",
        "payment_required",
        "internal",
    }
    for response in responses:
        body = response.json()
        assert list(body.keys()) == ["error"]
        assert sorted(body["error"].keys()) == ["code", "message"]
        assert body["error"]["code"] in known
        assert body["error"]["message"]


@scenario("uploads.unsupported-mime")
def test_uploads_unsupported_mime(conformance: Conformance, scenario_id: str):
    snap = conformance.client()
    with pytest.raises(SnapneditError) as caught:
        snap.upload(b"%PDF-1.4 not an image", mime="application/pdf")
    assert caught.value.status == 400
    assert caught.value.code is ErrorCode.UNSUPPORTED_MIME

    accepted = sorted({mime for op in snap.list_operations() for mime in op.accept})
    assert accepted == ["image/jpeg", "image/png", "image/webp"]


# ---------------------------------------------------------------- embed --


@scenario("embed.session")
def test_embed_session(conformance: Conformance, scenario_id: str):
    anon = conformance.client(None)
    session = anon.embed.create_session(conformance.publishable_key, "http://localhost:3000")
    assert session.token
    assert in_the_future(session.expires_at)

    with pytest.raises(SnapneditError) as caught:
        anon.embed.create_session(conformance.publishable_key, "https://not-allowed.example")
    assert caught.value.status == 403
    assert caught.value.code is ErrorCode.FORBIDDEN

    with pytest.raises(SnapneditError) as caught:
        anon.embed.create_token(ttl_seconds=600)
    assert caught.value.status == 401
    assert caught.value.code is ErrorCode.UNAUTHORIZED

    snap = conformance.client()
    minted = snap.embed.create_token(ttl_seconds=600)
    assert minted.token
    assert in_the_future(minted.expires_at)

    as_embed = snap.with_token(minted.token)
    assert isinstance(as_embed.destinations.list(), list)


# ------------------------------------------------------------- webhooks --


@scenario("webhooks.signature")
def test_webhooks_signature(scenarios: dict[str, Any], scenario_id: str):
    vector = scenarios["webhookVector"]
    secret = vector["secret"]
    timestamp = vector["timestamp"]
    raw_body = vector["rawBody"]
    signature = vector["signature"]
    header = vector["signatureHeaderValue"]

    recomputed = hmac.new(
        secret.encode(), f"{timestamp}.{raw_body}".encode(), hashlib.sha256
    ).hexdigest()
    assert recomputed == signature
    assert header == f"t={timestamp},v1={signature}"

    assert Webhooks.verify(raw_body, header, secret) is True
    tampered = f"t={timestamp},v1={signature[:-1]}{'b' if signature.endswith('a') else 'a'}"
    assert Webhooks.verify(raw_body, tampered, secret) is False
    assert Webhooks.verify(raw_body, header, "wrong secret") is False
    assert Webhooks.verify(raw_body + " ", header, secret) is False
    assert Webhooks.verify(raw_body, "not a signature header", secret) is False
    assert Webhooks.verify(raw_body, f"v1={signature}", secret) is False
    assert Webhooks.verify(raw_body, f"t={timestamp}", secret) is False
    assert (
        Webhooks.verify(raw_body, header, secret, tolerance_seconds=300, now=timestamp + 10)
        is True
    )
    assert (
        Webhooks.verify(raw_body, header, secret, tolerance_seconds=300, now=timestamp + 3600)
        is False
    )

    event = Webhooks.construct_event(raw_body, header, secret)
    assert event.type == "job.succeeded"
    assert event.data.job_id == json.loads(raw_body)["data"]["jobId"]


# ----------------------------------------------------------------- meta --


@scenario("openapi.document")
def test_openapi_document(conformance: Conformance, scenario_id: str):
    response = conformance.raw.get("/openapi.json")
    assert response.status_code == 200
    assert "application/json" in response.headers["content-type"]
    spec = response.json()
    assert spec["openapi"] == "3.1.0"
    assert re.match(r"^\d+\.\d+\.\d+", spec["info"]["version"])
    for path in (
        "/operations",
        "/uploads",
        "/uploads/{assetId}/confirm",
        "/jobs",
        "/jobs/{id}",
        "/destinations",
        "/destinations/{id}",
        "/destinations/{id}/test",
        "/destinations/{id}/presign",
        "/embed/sessions",
        "/embed/tokens",
    ):
        assert path in spec["paths"]
    assert sorted(spec["components"]["securitySchemes"]) == ["ApiKey", "EmbedToken", "Session"]
    assert spec["components"]["schemas"]["OperationParams.resize-image"]["x-credit-cost"] == 0

    committed = (conformance.root / "docs" / "openapi.json").read_text(encoding="utf-8")
    assert response.text == committed


# ------------------------------------------------------------- coverage --


@pytest.mark.conformance
def test_the_async_client_runs_the_same_flow(conformance: Conformance):
    """Not a scenario: proof the async twin behaves identically end to end."""
    data = conformance.unique_image("async")

    async def main() -> None:
        async with AsyncSnapnedit(conformance.api_key, conformance.url, max_retries=0) as snap:
            operations = await snap.list_operations()
            assert len(operations) == 17
            result = await snap.run("remove-background", data, **POLL)
            assert result.downloaded is True
            assert result.output
            job = await snap.get_job(result.job_id)
            assert job.state == "succeeded"
            assert isinstance(await snap.destinations.list(), list)

    asyncio.run(main())


def test_every_scenario_in_scenarios_json_is_implemented(scenarios: dict[str, Any]):
    """A new scenario upstream shows up here as a red test, not as silence."""
    assert scenarios["version"] == 1
    assert sorted(IMPLEMENTED) == sorted(entry["id"] for entry in scenarios["scenarios"])
    assert len(IMPLEMENTED) == 25
