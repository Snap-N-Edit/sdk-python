"""Unit tests: what the client puts on the wire, with a fake transport."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from _fake import FakeApi, create_body, job_body, upload_routes
from snapnedit import (
    AsyncSnapnedit,
    PresignedPutDestination,
    SavedDestination,
    Snapnedit,
    UrlInput,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"payload"


def client(api: FakeApi, **kwargs: object) -> Snapnedit:
    return Snapnedit("sk_test", "http://api.test", transport=api.transport, **kwargs)  # type: ignore[arg-type]


def test_upload_sends_three_calls_and_no_auth_on_the_presigned_put():
    api = upload_routes(FakeApi())
    with client(api) as snap:
        result = snap.upload(PNG)

    assert result.asset_id == "asset-in"
    assert result.content_hash == "a" * 64
    assert result.bytes == 8

    created = api.sent("POST", "/uploads")[0]
    assert created.json == {"mime": "image/png", "bytes": len(PNG)}
    assert created.headers["authorization"] == "Bearer sk_test"

    put = api.sent("PUT", "/_local/uploads/x")[0]
    assert put.content == PNG
    assert put.headers["content-type"] == "image/png"
    assert "authorization" not in put.headers
    # The presigned path resolves against the ORIGIN, not the api prefix.
    assert put.url.startswith("http://api.test/_local/uploads/x")

    assert api.sent("POST", "/uploads/asset-in/confirm")[0].content == b""


def test_upload_accepts_a_path_and_a_file_object(tmp_path: Path):
    api = upload_routes(FakeApi())
    file = tmp_path / "in.png"
    file.write_bytes(PNG)
    with client(api) as snap:
        snap.upload(file)
        with file.open("rb") as handle:
            snap.upload(handle)
    assert [r.json["mime"] for r in api.sent("POST", "/uploads")] == ["image/png", "image/png"]


def test_upload_uses_an_explicit_mime_over_sniffing():
    api = upload_routes(FakeApi())
    with client(api) as snap:
        snap.upload(b"not an image", mime="image/webp")
    assert api.sent("POST", "/uploads")[0].json["mime"] == "image/webp"


def test_create_job_body_asset_input_and_default_params():
    api = FakeApi().on("POST", "/jobs", status=202, json_body=create_body())
    with client(api) as snap:
        created = snap.create_job("remove-background", "asset-in")
    assert created.job_id == "job-1"
    assert created.http_status == 202
    assert created.cached is False
    assert api.sent("POST", "/jobs")[0].json == {
        "operation": "remove-background",
        "params": {},
        "inputAssetId": "asset-in",
    }


def test_create_job_body_url_input():
    api = FakeApi().on(
        "POST", "/jobs", status=202, json_body=create_body(input={"kind": "url"})
    )
    with client(api) as snap:
        created = snap.create_job("remove-background", UrlInput("https://example.com/a.png"))
    assert created.input.kind == "url"
    assert api.sent("POST", "/jobs")[0].json["inputUrl"] == "https://example.com/a.png"
    assert "inputAssetId" not in api.sent("POST", "/jobs")[0].json


def test_destination_omitted_null_and_named_are_three_different_bodies():
    api = FakeApi().on("POST", "/jobs", status=202, json_body=create_body())
    with client(api) as snap:
        snap.create_job("remove-background", "a")
        snap.create_job("remove-background", "a", destination=None)
        snap.create_job("remove-background", "a", destination=SavedDestination("dest-1"))
        snap.create_job(
            "remove-background",
            "a",
            destination=PresignedPutDestination(
                "https://bucket/out.png", {"content-type": "image/png"}
            ),
        )
    bodies = [r.json for r in api.sent("POST", "/jobs")]
    assert "destination" not in bodies[0]
    assert bodies[1]["destination"] is None
    assert bodies[2]["destination"] == {"type": "saved", "id": "dest-1"}
    assert bodies[3]["destination"] == {
        "type": "presigned-put",
        "url": "https://bucket/out.png",
        "headers": {"content-type": "image/png"},
    }


def test_run_polls_until_settled_then_downloads():
    api = FakeApi()
    upload_routes(api)
    api.on("POST", "/jobs", status=202, json_body=create_body())
    api.sequence(
        "GET",
        "/jobs/job-1",
        [
            httpx.Response(200, json={"state": "queued", **_envelope()}),
            httpx.Response(200, json={"state": "processing", "startedAt": "t", **_envelope()}),
            httpx.Response(200, json=job_body()),
        ],
    )
    api.on(
        "GET",
        "/_local/results/out.png",
        content=b"result-bytes",
        headers={"content-type": "image/png"},
    )
    with client(api) as snap:
        result = snap.run("remove-background", PNG, poll_interval=0.0)

    assert result.downloaded is True
    assert result.output == b"result-bytes"
    assert result.mime == "image/png"
    assert len(api.sent("GET", "/jobs/job-1")) == 3
    assert "authorization" not in api.sent("GET", "/_local/results/out.png")[0].headers


def test_run_skips_the_download_for_a_delivered_destination():
    api = FakeApi()
    upload_routes(api)
    delivered = {
        "input": {"kind": "asset"},
        "destination": {"type": "presigned-put"},
        "delivery": {"status": "delivered", "attempts": 1, "deliveredAt": "t"},
    }
    api.on("POST", "/jobs", status=202, json_body=create_body(**delivered))
    api.on("GET", "/jobs/job-1", json_body=job_body(**delivered))
    api.on("GET", "/_local/results/out.png", content=b"result-bytes")
    with client(api) as snap:
        result = snap.run(
            "remove-background",
            PNG,
            destination=PresignedPutDestination("https://bucket/out.png"),
            poll_interval=0.0,
        )
        forced = snap.run(
            "remove-background",
            PNG,
            destination=PresignedPutDestination("https://bucket/out.png"),
            download=True,
            poll_interval=0.0,
        )

    assert result.downloaded is False
    assert result.output is None
    assert result.download is not None  # a presigned-put delivery keeps our copy
    assert result.delivery is not None and result.delivery.status == "delivered"
    assert forced.downloaded is True and forced.output == b"result-bytes"


def test_run_still_downloads_when_a_delivery_failed():
    api = FakeApi()
    upload_routes(api)
    failed = {
        "input": {"kind": "asset"},
        "destination": {"type": "presigned-put"},
        "delivery": {"status": "failed", "attempts": 3, "error": "403 from bucket"},
    }
    api.on("POST", "/jobs", status=202, json_body=create_body(**failed))
    api.on("GET", "/jobs/job-1", json_body=job_body(**failed))
    api.on("GET", "/_local/results/out.png", content=b"result-bytes")
    with client(api) as snap:
        result = snap.run(
            "remove-background",
            PNG,
            destination=PresignedPutDestination("https://bucket/out.png"),
            poll_interval=0.0,
        )
    assert result.downloaded is True


def test_run_reports_not_downloaded_when_the_local_copy_was_deleted():
    api = FakeApi()
    upload_routes(api)
    deleted = {
        "input": {"kind": "asset"},
        "destination": {"type": "saved", "id": "d1", "name": "exports"},
        "delivery": {
            "status": "delivered",
            "attempts": 1,
            "bucket": "b",
            "key": "k.png",
            "localCopyDeleted": True,
        },
    }
    api.on("POST", "/jobs", status=202, json_body=create_body(**deleted))
    api.on("GET", "/jobs/job-1", json_body=job_body(download=None, **deleted))
    with client(api) as snap:
        result = snap.run(
            "remove-background", PNG, destination=SavedDestination("d1"),
            download=True, poll_interval=0.0,
        )
    assert result.downloaded is False
    assert result.download is None
    assert result.delivery is not None and result.delivery.key == "k.png"


def test_run_waits_for_a_pending_delivery_on_a_cache_hit():
    api = FakeApi()
    upload_routes(api)
    pending = {
        "input": {"kind": "asset"},
        "destination": {"type": "saved", "id": "d1"},
        "delivery": {"status": "pending", "attempts": 0},
    }
    # A cache hit answers 200 with a terminal status but an unsettled delivery.
    api.on(
        "POST",
        "/jobs",
        status=200,
        json_body=create_body(
            status={"state": "succeeded", "outputAssetId": "o", "download": None}, **pending
        ),
    )
    api.on(
        "GET",
        "/jobs/job-1",
        json_body=job_body(
            input={"kind": "asset"},
            destination={"type": "saved", "id": "d1"},
            delivery={"status": "delivered", "attempts": 1, "key": "k.png"},
        ),
    )
    api.on("GET", "/_local/results/out.png", content=b"bytes")
    with client(api) as snap:
        result = snap.run(
            "remove-background", PNG, destination=SavedDestination("d1"), poll_interval=0.0
        )
    assert api.sent("GET", "/jobs/job-1")  # polled rather than trusting the cache hit
    assert result.delivery is not None and result.delivery.status == "delivered"


def test_operations_and_destination_bodies_are_camel_cased():
    api = FakeApi().on(
        "POST",
        "/destinations",
        status=201,
        json_body={"destination": _destination()},
    )
    with client(api) as snap:
        dest = snap.destinations.create(
            name="exports",
            provider="s3-compatible",
            bucket="my-bucket",
            access_key_id="AKIAEXAMPLE1234",
            secret_access_key="secret",
            key_prefix="out/",
            force_path_style=True,
            is_default=True,
        )
    assert dest.access_key_id_last4 == "1234"
    assert api.sent("POST", "/destinations")[0].json == {
        "name": "exports",
        "provider": "s3-compatible",
        "bucket": "my-bucket",
        "accessKeyId": "AKIAEXAMPLE1234",
        "secretAccessKey": "secret",
        "keyPrefix": "out/",
        "forcePathStyle": True,
        "isDefault": True,
    }


def test_destination_patch_omits_untouched_fields_but_keeps_explicit_nulls():
    api = FakeApi().on("PATCH", "/destinations/d1", json_body={"destination": _destination()})
    with client(api) as snap:
        snap.destinations.update("d1", name="renamed", endpoint=None)
    assert api.sent("PATCH", "/destinations/d1")[0].json == {
        "name": "renamed",
        "endpoint": None,
    }


def test_delete_destination_accepts_204_with_no_body():
    api = FakeApi().on("DELETE", "/destinations/d1", status=204, content=b"")
    with client(api) as snap:
        assert snap.destinations.delete("d1") is None


def test_embed_session_is_sent_without_a_bearer():
    api = FakeApi().on(
        "POST", "/embed/sessions", json_body={"token": "et_1", "expiresAt": "2026-01-01T00:00:00Z"}
    )
    with client(api) as snap:
        token = snap.embed.create_session("pk_live_x", "http://localhost:3000")
    assert token.token == "et_1"
    sent = api.sent("POST", "/embed/sessions")[0]
    assert "authorization" not in sent.headers
    assert sent.json == {"publishableKey": "pk_live_x", "hostOrigin": "http://localhost:3000"}


def test_embed_token_body_is_camel_cased_and_authenticated():
    api = FakeApi().on(
        "POST", "/embed/tokens", json_body={"token": "et_2", "expiresAt": "2026-01-01T00:00:00Z"}
    )
    with client(api) as snap:
        snap.embed.create_token(ttl_seconds=600, allowed_operations=["upscale"], max_credits=5)
    sent = api.sent("POST", "/embed/tokens")[0]
    assert sent.headers["authorization"] == "Bearer sk_test"
    assert sent.json == {"ttlSeconds": 600, "allowedOperations": ["upscale"], "maxCredits": 5}


def test_base_url_keeps_its_path_prefix_for_api_calls():
    api = FakeApi().on("GET", "/api/operations", json_body=[])
    snap = Snapnedit("sk", "https://snapnedit.com/api/", transport=api.transport)
    with snap:
        snap.list_operations()
    assert api.sent("GET", "/api/operations")[0].url == "https://snapnedit.com/api/operations"


def test_async_client_mirrors_the_sync_one():
    api = FakeApi()
    upload_routes(api)
    api.on("POST", "/jobs", status=202, json_body=create_body())
    api.on("GET", "/jobs/job-1", json_body=job_body())
    api.on(
        "GET",
        "/_local/results/out.png",
        content=b"async-bytes",
        headers={"content-type": "image/png"},
    )

    async def main() -> None:
        async with AsyncSnapnedit(
            "sk_test", "http://api.test", transport=api.async_transport
        ) as snap:
            result = await snap.run("remove-background", PNG, poll_interval=0.0)
            assert result.output == b"async-bytes"
            assert result.mime == "image/png"
            job = await snap.get_job("job-1")
            assert job.state == "succeeded"

    asyncio.run(main())
    assert json.loads(api.sent("POST", "/jobs")[0].content)["operation"] == "remove-background"


def _envelope() -> dict[str, object]:
    return {"input": {"kind": "asset"}, "destination": None, "delivery": None}


def _destination() -> dict[str, object]:
    return {
        "id": "d1",
        "name": "exports",
        "provider": "s3-compatible",
        "bucket": "my-bucket",
        "region": None,
        "endpoint": None,
        "forcePathStyle": True,
        "keyPrefix": "out/",
        "accessKeyIdLast4": "1234",
        "isDefault": True,
        "deleteAfterDelivery": False,
        "lastTest": None,
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
    }


@pytest.mark.parametrize("factor", ["2", "4"])
def test_params_are_passed_through_verbatim(factor: str):
    api = FakeApi().on("POST", "/jobs", status=202, json_body=create_body())
    with client(api) as snap:
        snap.create_job("upscale", "asset-in", {"factor": factor})
    assert api.sent("POST", "/jobs")[0].json["params"] == {"factor": factor}
