"""A scriptable `httpx.MockTransport` that records every request it served."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx

Responder = Callable[[httpx.Request], httpx.Response]


@dataclass
class Recorded:
    method: str
    url: str
    path: str
    headers: dict[str, str]
    content: bytes

    @property
    def json(self) -> Any:
        return json.loads(self.content.decode()) if self.content else None


@dataclass
class FakeApi:
    """Routes `(METHOD, path)` to a response, or to a queue of responses."""

    routes: dict[tuple[str, str], list[Responder]] = field(default_factory=dict)
    requests: list[Recorded] = field(default_factory=list)

    def on(
        self,
        method: str,
        path: str,
        *,
        status: int = 200,
        json_body: Any = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        times: int = 1_000_000,
    ) -> FakeApi:
        def respond(_: httpx.Request) -> httpx.Response:
            if content is not None:
                return httpx.Response(status, content=content, headers=headers or {})
            return httpx.Response(status, json=json_body, headers=headers or {})

        self.routes.setdefault((method.upper(), path), []).extend([respond] * min(times, 64))
        return self

    def sequence(self, method: str, path: str, responses: Iterable[httpx.Response]) -> FakeApi:
        queued = list(responses)

        def make(response: httpx.Response) -> Responder:
            return lambda _: response

        self.routes.setdefault((method.upper(), path), []).extend(make(r) for r in queued)
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            Recorded(
                method=request.method,
                url=str(request.url),
                path=request.url.path,
                headers={k.lower(): v for k, v in request.headers.items()},
                content=request.content,
            )
        )
        key = (request.method.upper(), request.url.path)
        queue = self.routes.get(key)
        if not queue:
            return httpx.Response(
                404, json={"error": {"code": "not_found", "message": f"no route for {key}"}}
            )
        responder = queue.pop(0) if len(queue) > 1 else queue[0]
        return responder(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    @property
    def async_transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def sent(self, method: str, path: str) -> list[Recorded]:
        return [r for r in self.requests if r.method == method.upper() and r.path == path]


def job_body(**overrides: Any) -> dict[str, Any]:
    """Build a `GET /jobs/{id}` body in the succeeded shape."""
    body: dict[str, Any] = {
        "state": "succeeded",
        "outputAssetId": "asset-out",
        "download": {"url": "/_local/results/out.png?sig=x", "expiresAt": "2026-01-01T00:00:00Z"},
        "input": {"kind": "asset"},
        "destination": None,
        "delivery": None,
    }
    body.update(overrides)
    return body


def create_body(**overrides: Any) -> dict[str, Any]:
    """Build a `POST /jobs` body."""
    body: dict[str, Any] = {
        "jobId": "job-1",
        "status": {"state": "queued"},
        "input": {"kind": "asset"},
        "destination": None,
        "delivery": None,
    }
    body.update(overrides)
    return body


def upload_routes(api: FakeApi, asset_id: str = "asset-in") -> FakeApi:
    """Wire the three-step upload flow."""
    api.on(
        "POST",
        "/uploads",
        json_body={
            "assetId": asset_id,
            "upload": {"url": "/_local/uploads/x?sig=y", "expiresAt": "2026-01-01T00:00:00Z"},
        },
    )
    api.on("PUT", "/_local/uploads/x", status=204, content=b"")
    api.on(
        "POST",
        f"/uploads/{asset_id}/confirm",
        json_body={"assetId": asset_id, "contentHash": "a" * 64, "bytes": 8},
    )
    return api
