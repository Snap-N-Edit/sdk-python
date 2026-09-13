"""Unit tests: webhook signature verification against the fixed cross-language vector."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from snapnedit import (
    WEBHOOK_SIGNATURE_HEADER,
    SnapneditSignatureError,
    Webhooks,
)

SECRET = "whsec_conformance_fixed_test_vector"
TIMESTAMP = 1700000000
RAW_BODY = (
    '{"id":"whd_00000000-0000-4000-8000-000000000001","type":"job.succeeded",'
    '"created":1700000000,"data":{"jobId":"9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",'
    '"operation":"remove-background","status":"succeeded",'
    '"outputAssetId":"3f2504e0-4f89-41d3-9a0c-0305e82c3301",'
    '"download":"https://snapnedit.com/_local/results/abc.png?exp=1700003600&sig=deadbeef",'
    '"input":{"kind":"asset"},"destination":null,"delivery":null}}'
)
SIGNATURE = "8a587f5869207b7da3be52c9d2695b0201c442c27cc37d2176551349e6461d90"
HEADER = f"t={TIMESTAMP},v1={SIGNATURE}"


def test_the_header_name_is_the_documented_one():
    assert WEBHOOK_SIGNATURE_HEADER == "X-Snapnedit-Signature"
    assert Webhooks.SIGNATURE_HEADER == WEBHOOK_SIGNATURE_HEADER


def test_the_vector_reproduces_bit_for_bit():
    expected = hmac.new(
        SECRET.encode(), f"{TIMESTAMP}.{RAW_BODY}".encode(), hashlib.sha256
    ).hexdigest()
    assert expected == SIGNATURE


def test_a_correct_header_verifies_as_str_and_as_bytes():
    assert Webhooks.verify(RAW_BODY, HEADER, SECRET) is True
    assert Webhooks.verify(RAW_BODY.encode(), HEADER, SECRET) is True
    assert Webhooks.verify(bytearray(RAW_BODY.encode()), HEADER, SECRET) is True


@pytest.mark.parametrize(
    ("payload", "header", "secret"),
    [
        (RAW_BODY, f"t={TIMESTAMP},v1={SIGNATURE[:-1]}b", SECRET),  # one hex char changed
        (RAW_BODY, HEADER, "wrong secret"),
        (RAW_BODY + " ", HEADER, SECRET),  # a re-serialized body
        (RAW_BODY, "not a signature header", SECRET),
        (RAW_BODY, f"v1={SIGNATURE}", SECRET),  # no t=
        (RAW_BODY, f"t={TIMESTAMP}", SECRET),  # no v1=
        (RAW_BODY, "", SECRET),
        (RAW_BODY, f"t=nonsense,v1={SIGNATURE}", SECRET),
    ],
)
def test_anything_wrong_returns_false_and_never_raises(payload, header, secret):
    assert Webhooks.verify(payload, header, secret) is False


def test_uppercase_hex_is_accepted_and_unknown_keys_are_ignored():
    assert Webhooks.verify(RAW_BODY, f"t={TIMESTAMP},v1={SIGNATURE.upper()}", SECRET) is True
    assert Webhooks.verify(RAW_BODY, f"t={TIMESTAMP},v0=x,v1={SIGNATURE}", SECRET) is True


def test_a_rotation_window_may_carry_several_v1_values():
    rotated = f"t={TIMESTAMP},v1={'0' * 64},v1={SIGNATURE}"
    assert Webhooks.verify(RAW_BODY, rotated, SECRET) is True


def test_the_freshness_window_is_opt_in():
    assert Webhooks.verify(RAW_BODY, HEADER, SECRET, now=TIMESTAMP + 10_000) is True
    assert (
        Webhooks.verify(
            RAW_BODY, HEADER, SECRET, tolerance_seconds=300, now=TIMESTAMP + 10
        )
        is True
    )
    assert (
        Webhooks.verify(
            RAW_BODY, HEADER, SECRET, tolerance_seconds=300, now=TIMESTAMP + 3600
        )
        is False
    )
    assert (
        Webhooks.verify(
            RAW_BODY, HEADER, SECRET, tolerance_seconds=300, now=TIMESTAMP - 3600
        )
        is False
    )


def test_construct_event_verifies_then_parses():
    event = Webhooks.construct_event(RAW_BODY, HEADER, SECRET)
    assert event.id == "whd_00000000-0000-4000-8000-000000000001"
    assert event.type == "job.succeeded"
    assert event.created == TIMESTAMP
    assert event.data.job_id == "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"
    assert event.data.operation == "remove-background"
    assert event.data.output_asset_id == "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
    assert event.data.input.kind == "asset"
    assert event.data.destination is None
    assert event.data.delivery is None
    assert event.raw["type"] == "job.succeeded"


def test_construct_event_raises_on_a_bad_signature():
    with pytest.raises(SnapneditSignatureError):
        Webhooks.construct_event(RAW_BODY, f"t={TIMESTAMP},v1={'0' * 64}", SECRET)


def test_construct_event_parses_a_failed_delivery_envelope():
    body = (
        '{"id":"whd_2","type":"job.succeeded","created":1,"data":{"jobId":"j",'
        '"operation":"upscale","status":"succeeded","outputAssetId":"o",'
        '"input":{"kind":"url"},"destination":{"type":"presigned-put"},'
        '"delivery":{"status":"failed","attempts":3,"error":"403"}}}'
    )
    signature = hmac.new(SECRET.encode(), f"1.{body}".encode(), hashlib.sha256).hexdigest()
    event = Webhooks.construct_event(body, f"t=1,v1={signature}", SECRET)
    assert event.data.input.kind == "url"
    assert event.data.destination is not None and event.data.destination.type == "presigned-put"
    assert event.data.delivery is not None and event.data.delivery.status == "failed"
    assert event.data.delivery.attempts == 3
