"""Unit tests: parsing wire shapes into typed views."""

from __future__ import annotations

import pytest

from snapnedit import (
    ERROR_CODES,
    OPERATION_IDS,
    UNSET,
    CreateJobResult,
    ErrorCode,
    JobView,
    OperationMetadata,
    SnapneditError,
    StorageDestination,
    credit_cost,
    requires_mask,
)
from snapnedit._inputs import read_image, sniff_mime


def view(**overrides: object) -> JobView:
    body: dict[str, object] = {
        "state": "succeeded",
        "outputAssetId": "o",
        "download": {"url": "/x", "expiresAt": "t"},
        "input": {"kind": "asset"},
        "destination": None,
        "delivery": None,
    }
    body.update(overrides)
    return JobView.from_wire("job-1", body, "GET /jobs/job-1")


def test_every_job_state_parses():
    assert view(state="queued", outputAssetId=None, download=None).state == "queued"
    processing = view(state="processing", startedAt="t0", outputAssetId=None, download=None)
    assert processing.status.started_at == "t0"
    assert processing.terminal is False
    failed = view(state="failed", errorCode="too_large", message="nope",
                  outputAssetId=None, download=None)
    assert failed.error_code is ErrorCode.TOO_LARGE
    assert failed.message == "nope"
    assert failed.terminal is True
    assert view(state="canceled", outputAssetId=None, download=None).terminal is True


def test_a_succeeded_job_may_carry_a_null_download():
    parsed = view(download=None)
    assert parsed.state == "succeeded"
    assert parsed.download is None
    assert parsed.output_asset_id == "o"


def test_an_unknown_state_is_a_malformed_response():
    with pytest.raises(SnapneditError) as caught:
        view(state="dancing")
    assert caught.value.code is ErrorCode.INTERNAL


def test_settled_waits_for_a_pending_delivery_but_not_a_delivered_one():
    pending = view(destination={"type": "saved", "id": "d"},
                   delivery={"status": "pending", "attempts": 0})
    assert pending.terminal is True
    assert pending.settled is False
    done = view(destination={"type": "saved", "id": "d", "name": "exports"},
                delivery={"status": "delivered", "attempts": 1, "key": "k", "bucket": "b"})
    assert done.settled is True
    assert done.destination is not None and done.destination.name == "exports"
    assert done.delivery is not None and done.delivery.key == "k"


def test_an_unknown_delivery_status_degrades_to_pending():
    parsed = view(delivery={"status": "sideways", "attempts": 2})
    assert parsed.delivery is not None
    assert parsed.delivery.status == "pending"
    assert parsed.delivery.attempts == 2


def test_a_missing_envelope_reads_as_an_asset_input():
    parsed = JobView.from_wire("j", {"state": "queued"}, "GET /jobs/j")
    assert parsed.input.kind == "asset"
    assert parsed.destination is None and parsed.delivery is None


def test_an_unrecognized_destination_reads_as_none():
    assert view(destination={"type": "carrier-pigeon"}).destination is None
    assert view(destination={"type": "saved"}).destination is None


def test_create_job_result_flags_a_cache_hit():
    body = {
        "jobId": "j",
        "status": {"state": "succeeded", "outputAssetId": "o", "download": None},
        "input": {"kind": "url"},
        "destination": None,
        "delivery": None,
    }
    hit = CreateJobResult.from_wire(body, "POST /jobs", 200)
    assert hit.cached is True
    assert hit.state == "succeeded"
    assert hit.as_view().input.kind == "url"
    assert CreateJobResult.from_wire(body, "POST /jobs", 202).cached is False


def test_destination_views_never_leak_the_secret():
    parsed = StorageDestination.from_wire(
        {
            "id": "d1",
            "name": "exports",
            "provider": "cloudflare-r2",
            "bucket": "b",
            "region": None,
            "endpoint": "https://r2.example",
            "forcePathStyle": False,
            "keyPrefix": "p/",
            "accessKeyIdLast4": "1234",
            "isDefault": True,
            "deleteAfterDelivery": True,
            "lastTest": {"status": "ok", "at": "t"},
            "createdAt": "t",
            "updatedAt": "t",
        },
        "GET /destinations",
    )
    assert parsed.provider == "cloudflare-r2"
    assert parsed.last_test is not None and parsed.last_test.status == "ok"
    assert "secret" not in repr(parsed).lower()


def test_an_unknown_storage_provider_is_refused():
    with pytest.raises(SnapneditError):
        StorageDestination.from_wire({"provider": "ftp"}, "GET /destinations")


def test_operation_metadata_parses():
    parsed = OperationMetadata.from_wire(
        {
            "id": "upscale",
            "label": "Upscale",
            "accept": ["image/png"],
            "maxInputDimension": 4096,
            "paramsJsonSchema": {"properties": {"factor": {"enum": ["2", "4"]}}},
            "requiresMask": False,
            "description": "d",
            "seoTitle": "s",
        },
        "GET /operations",
    )
    assert parsed.max_input_dimension == 4096
    assert parsed.requires_mask is False


def test_the_static_catalog_matches_the_documented_one():
    assert len(OPERATION_IDS) == 17
    assert credit_cost("resize-image") == 0
    assert credit_cost("remove-background") == 1
    assert credit_cost("upscale") == 2
    assert credit_cost("not-an-op") == -1
    assert requires_mask("magic-eraser") is True
    assert requires_mask("upscale") is False


def test_the_error_code_set_is_closed_and_coercible():
    assert len(ERROR_CODES) == 13
    assert ErrorCode.coerce("payment_required") is ErrorCode.PAYMENT_REQUIRED
    assert ErrorCode.coerce(None) is ErrorCode.INTERNAL
    assert str(ErrorCode.NOT_FOUND) == "not_found"


def test_unset_is_a_singleton_and_falsy():
    from snapnedit.models import UnsetType

    assert UnsetType() is UNSET
    assert bool(UNSET) is False
    assert repr(UNSET) == "UNSET"


def test_image_inputs_are_read_and_sniffed(tmp_path):
    png = b"\x89PNG\r\n\x1a\n rest"
    jpeg = b"\xff\xd8\xff\xe0 rest"
    webp = b"RIFF\x00\x00\x00\x00WEBPVP8 "
    assert sniff_mime(png) == "image/png"
    assert sniff_mime(jpeg) == "image/jpeg"
    assert sniff_mime(webp) == "image/webp"
    assert sniff_mime(b"nope") is None

    assert read_image(png) == (png, "image/png")
    assert read_image(b"nope", "image/webp") == (b"nope", "image/webp")
    assert read_image(bytearray(jpeg))[1] == "image/jpeg"

    path = tmp_path / "a.png"
    path.write_bytes(png)
    assert read_image(path) == (png, "image/png")
    assert read_image(str(path))[1] == "image/png"

    unknown = tmp_path / "a.tiff"
    unknown.write_bytes(b"II*\x00rest")
    assert read_image(unknown)[1] == "image/tiff"

    with pytest.raises(SnapneditError):
        read_image(b"")
