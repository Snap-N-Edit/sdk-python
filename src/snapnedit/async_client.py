"""The asynchronous twin: `AsyncSnapnedit`.

Same surface, same request layer (`_endpoints`), same decisions (`_flows`) and
the same error mapping as :mod:`snapnedit.client` — the only difference is that
the I/O is awaited.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import Any

import httpx

from . import _endpoints as ep
from ._flows import Deadline, job_failure, should_download
from ._inputs import ImageInput, as_url_input, read_image
from ._transport import (
    DEFAULT_BASE_URL,
    Request,
    RetryPolicy,
    check_response,
    parse_json,
    resolve_url,
    retry_after_seconds,
)
from ._version import __version__
from .errors import ErrorCode, SnapneditError
from .models import (
    UNSET,
    CreateJobResult,
    Destination,
    DestinationPresign,
    DestinationTestResult,
    EmbedToken,
    JobView,
    OperationMetadata,
    RunResult,
    SignedUrl,
    StorageDestination,
    UnsetType,
    UploadResult,
    UrlInput,
)

__all__ = ["AsyncSnapnedit"]

USER_AGENT = f"snapnedit-python/{__version__}"

DEFAULT_POLL_INTERVAL = 1.0
DEFAULT_JOB_TIMEOUT = 120.0


class AsyncSnapnedit:
    """An asyncio snapnedit api client.

    ```python
    import asyncio
    from snapnedit import AsyncSnapnedit

    async def main() -> None:
        async with AsyncSnapnedit(api_key="sk_live_…") as snap:
            result = await snap.run("upscale", "photo.jpg", {"factor": "2"})
            print(result.mime, len(result.output or b""))

    asyncio.run(main())
    ```
    """

    destinations: AsyncDestinationsClient
    """Saved storage destinations: list, create, update, delete, test, presign_upload."""
    embed: AsyncEmbedClient
    """Embed tokens: `create_session` (publishable key) and `create_token` (api key)."""
    designs: AsyncDesignsClient
    """Declarative designs: `create`, `create_pages`, `render`."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        timeout: float = 60.0,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
        http_client: httpx.AsyncClient | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Build a client. See :class:`snapnedit.Snapnedit` for the arguments."""
        self._api_key = api_key if api_key is not None else os.environ.get("SNAPNEDIT_API_KEY")
        self._base_url = (
            base_url or os.environ.get("SNAPNEDIT_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self._retry = RetryPolicy(max_retries=max(max_retries, 0))
        self._user_agent = user_agent or USER_AGENT
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout, transport=transport)
        self.destinations = AsyncDestinationsClient(self)
        self.embed = AsyncEmbedClient(self)
        self.designs = AsyncDesignsClient(self)

    # -- plumbing ----------------------------------------------------------

    @property
    def base_url(self) -> str:
        """The api origin this client talks to."""
        return self._base_url

    def with_token(self, token: str) -> AsyncSnapnedit:
        """Build a second client for the same api, authenticating with `token` instead."""
        return AsyncSnapnedit(
            token,
            self._base_url,
            max_retries=self._retry.max_retries,
            http_client=self._http,
            user_agent=self._user_agent,
        )

    async def aclose(self) -> None:
        """Close the underlying connection pool (unless you supplied the client)."""
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> AsyncSnapnedit:
        """Enter an `async with` block."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the client on the way out of an `async with` block."""
        await self.aclose()

    def _headers(self, request: Request) -> dict[str, str]:
        headers = {key.lower(): value for key, value in request.headers.items()}
        headers.setdefault("user-agent", self._user_agent)
        headers.setdefault("accept", "application/json")
        if request.auth and self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        return headers

    async def send(self, request: Request) -> httpx.Response:
        """Send one prepared :class:`Request`, retrying and raising per the shared policy."""
        url = resolve_url(self._base_url, request)
        headers = self._headers(request)
        attempt = 0
        while True:
            try:
                response = await self._http.request(
                    request.method,
                    url,
                    headers=headers,
                    content=request.content,
                    json=request.json_body if request.content is None else None,
                )
            except httpx.HTTPError as exc:
                if request.retryable and attempt < self._retry.max_retries:
                    attempt += 1
                    await asyncio.sleep(self._retry.delay(attempt))
                    continue
                raise SnapneditError(
                    ErrorCode.INTERNAL, f"network error calling {url}: {exc}", 0
                ) from exc
            if (
                request.retryable
                and attempt < self._retry.max_retries
                and self._retry.should_retry_status(response.status_code)
            ):
                attempt += 1
                await asyncio.sleep(
                    self._retry.delay(attempt, retry_after_seconds(dict(response.headers)))
                )
                continue
            check_response(response.status_code, response.content, url)
            return response

    async def _json(self, request: Request) -> tuple[Any, str, int]:
        response = await self.send(request)
        url = str(response.request.url)
        return parse_json(response.content, response.status_code, url), url, response.status_code

    # -- catalog -----------------------------------------------------------

    async def list_operations(self) -> list[OperationMetadata]:
        """`GET /operations` — every operation, its accepted mimes and params schema."""
        payload, url, _ = await self._json(ep.list_operations())
        return ep.parse_operations(payload, url)

    # -- uploads -----------------------------------------------------------

    async def upload(self, image: ImageInput, mime: str | None = None) -> UploadResult:
        """Upload one asset: `POST /uploads` -> presigned `PUT` -> `POST /confirm`."""
        data, resolved_mime = read_image(image, mime)
        payload, url, _ = await self._json(ep.create_upload(resolved_mime, len(data)))
        asset_id, signed = ep.parse_create_upload(payload, url)
        await self.send(ep.put_upload(signed.url, data, resolved_mime))
        payload, url, _ = await self._json(ep.confirm_upload(asset_id))
        confirmed_id, content_hash, size = ep.parse_confirm_upload(payload, url)
        return UploadResult(asset_id=confirmed_id, content_hash=content_hash, bytes=size)

    # -- jobs --------------------------------------------------------------

    async def create_job(
        self,
        operation: str,
        input: str | UrlInput | Mapping[str, Any],
        params: Mapping[str, Any] | None = None,
        *,
        destination: Destination | UnsetType | None = UNSET,
    ) -> CreateJobResult:
        """`POST /jobs`. See :meth:`snapnedit.Snapnedit.create_job`."""
        url_input = as_url_input(input)
        job_input: str | UrlInput = url_input if url_input is not None else str(input)
        payload, url, status = await self._json(
            ep.create_job(operation, job_input, params, destination)
        )
        return ep.parse_create_job(payload, url, status)

    async def get_job(self, job_id: str) -> JobView:
        """`GET /jobs/{id}` — one poll. Someone else's job id is a 404, never a 403."""
        payload, url, _ = await self._json(ep.get_job(job_id))
        return ep.parse_job(payload, url, job_id)

    async def wait_for_job(
        self,
        job_id: str,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        timeout: float = DEFAULT_JOB_TIMEOUT,
    ) -> JobView:
        """Poll until the job is settled — terminal AND any delivery resolved."""
        deadline = Deadline.start(timeout)
        while True:
            view = await self.get_job(job_id)
            if view.settled:
                return view
            await asyncio.sleep(deadline.sleep_for(poll_interval, job_id))

    async def run(
        self,
        operation: str,
        input: ImageInput | UrlInput | Mapping[str, Any],
        params: Mapping[str, Any] | None = None,
        *,
        mask: ImageInput | None = None,
        mime: str | None = None,
        destination: Destination | UnsetType | None = UNSET,
        download: bool | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        timeout: float = DEFAULT_JOB_TIMEOUT,
    ) -> RunResult:
        """Upload, run, poll and download in one call. See :meth:`snapnedit.Snapnedit.run`."""
        url_input = as_url_input(input)
        job_input: str | UrlInput
        if url_input is not None:
            job_input = url_input
        else:
            data, resolved_mime = read_image(input, mime)  # type: ignore[arg-type]
            job_input = (await self.upload(data, resolved_mime)).asset_id

        job_params = dict(params or {})
        if mask is not None:
            mask_data, mask_mime = read_image(mask)
            job_params["maskAssetId"] = (await self.upload(mask_data, mask_mime)).asset_id

        created = await self.create_job(operation, job_input, job_params, destination=destination)
        view = created.as_view()
        if not view.settled:
            view = await self.wait_for_job(
                created.job_id, poll_interval=poll_interval, timeout=timeout
            )

        failure = job_failure(view)
        if failure is not None:
            raise failure

        if should_download(download, destination, view) and view.download is not None:
            output, output_mime = await self._download(view.download)
            return RunResult(
                job_id=view.job_id,
                downloaded=True,
                input=view.input,
                destination=view.destination,
                delivery=view.delivery,
                download=view.download,
                output=output,
                mime=output_mime,
            )
        return RunResult(
            job_id=view.job_id,
            downloaded=False,
            input=view.input,
            destination=view.destination,
            delivery=view.delivery,
            download=view.download,
        )

    async def download_result(self, source: JobView | RunResult | SignedUrl | str) -> bytes:
        """Fetch result bytes from a job, a run result or a presigned url."""
        signed: SignedUrl | str | None = (
            source.download if isinstance(source, (JobView, RunResult)) else source
        )
        if signed is None:
            raise SnapneditError(
                ErrorCode.NOT_FOUND,
                "there is no downloadable copy of this result (delivered and deleted)",
            )
        return (await self._download(signed))[0]

    async def _download(self, signed: SignedUrl | str) -> tuple[bytes, str]:
        response = await self.send(ep.download(signed))
        return response.content, response.headers.get("content-type", "application/octet-stream")


class AsyncDestinationsClient:
    """Saved storage destinations — `client.destinations`."""

    def __init__(self, client: AsyncSnapnedit) -> None:
        """Bind to its parent client."""
        self._client = client

    async def list(self) -> list[StorageDestination]:
        """`GET /destinations` — credential-free views of every saved bucket."""
        payload, url, _ = await self._client._json(ep.list_destinations())
        return ep.parse_destination_list(payload, url)

    async def create(
        self,
        *,
        name: str,
        provider: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        region: str | UnsetType | None = UNSET,
        endpoint: str | UnsetType | None = UNSET,
        account_id: str | UnsetType = UNSET,
        force_path_style: bool | UnsetType = UNSET,
        key_prefix: str | UnsetType = UNSET,
        is_default: bool | UnsetType = UNSET,
        delete_after_delivery: bool | UnsetType = UNSET,
    ) -> StorageDestination:
        """`POST /destinations` — save a bucket. The secret key is never echoed back."""
        body = ep.destination_body(
            name=name,
            provider=provider,
            bucket=bucket,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            region=region,
            endpoint=endpoint,
            account_id=account_id,
            force_path_style=force_path_style,
            key_prefix=key_prefix,
            is_default=is_default,
            delete_after_delivery=delete_after_delivery,
        )
        payload, url, _ = await self._client._json(ep.create_destination(body))
        return ep.parse_destination(payload, url)

    async def update(
        self,
        destination_id: str,
        *,
        name: str | UnsetType = UNSET,
        bucket: str | UnsetType = UNSET,
        region: str | UnsetType | None = UNSET,
        endpoint: str | UnsetType | None = UNSET,
        force_path_style: bool | UnsetType = UNSET,
        key_prefix: str | UnsetType = UNSET,
        access_key_id: str | UnsetType = UNSET,
        secret_access_key: str | UnsetType = UNSET,
        is_default: bool | UnsetType = UNSET,
        delete_after_delivery: bool | UnsetType = UNSET,
    ) -> StorageDestination:
        """`PATCH /destinations/{id}`."""
        body = ep.destination_body(
            name=name,
            bucket=bucket,
            region=region,
            endpoint=endpoint,
            force_path_style=force_path_style,
            key_prefix=key_prefix,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            is_default=is_default,
            delete_after_delivery=delete_after_delivery,
        )
        payload, url, _ = await self._client._json(ep.update_destination(destination_id, body))
        return ep.parse_destination(payload, url)

    async def delete(self, destination_id: str) -> None:
        """`DELETE /destinations/{id}` — 204."""
        await self._client.send(ep.delete_destination(destination_id))

    async def test(self, destination_id: str) -> DestinationTestResult:
        """`POST /destinations/{id}/test` — a real write-then-delete probe."""
        payload, url, _ = await self._client._json(ep.test_destination(destination_id))
        return ep.parse_destination_test(payload, url)

    async def presign_upload(
        self, destination_id: str, *, ext: str, content_type: str
    ) -> DestinationPresign:
        """`POST /destinations/{id}/presign` — a 15-minute PUT into your own bucket."""
        payload, url, _ = await self._client._json(
            ep.presign_destination_upload(destination_id, ext, content_type)
        )
        return ep.parse_destination_presign(payload, url)


class AsyncEmbedClient:
    """Embedded-editor credentials — `client.embed`."""

    def __init__(self, client: AsyncSnapnedit) -> None:
        """Bind to its parent client."""
        self._client = client

    async def create_session(self, publishable_key: str, host_origin: str) -> EmbedToken:
        """`POST /embed/sessions` — exchange a `pk_` key for a short-lived token."""
        payload, url, _ = await self._client._json(
            ep.create_embed_session(publishable_key, host_origin)
        )
        return ep.parse_embed_token(payload, url)

    async def create_token(
        self,
        *,
        ttl_seconds: int | UnsetType = UNSET,
        end_user_id: str | UnsetType = UNSET,
        max_credits: int | UnsetType = UNSET,
        allowed_operations: Sequence[str] | UnsetType = UNSET,
        origin: str | UnsetType = UNSET,
    ) -> EmbedToken:
        """`POST /embed/tokens` — mint a scoped embed token from your own server."""
        payload, url, _ = await self._client._json(
            ep.create_embed_token(
                ttl_seconds=ttl_seconds,
                end_user_id=end_user_id,
                max_credits=max_credits,
                allowed_operations=allowed_operations,
                origin=origin,
            )
        )
        return ep.parse_embed_token(payload, url)


class AsyncDesignsClient:
    """Declarative designs — `client.designs`."""

    def __init__(self, client: AsyncSnapnedit) -> None:
        """Bind to its parent client."""
        self._client = client

    async def create(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        """`POST /designs` — compile a design spec into one editor document."""
        payload, url, _ = await self._client._json(ep.create_design(spec))
        return ep.parse_design(payload, url)

    async def create_pages(self, spec: Mapping[str, Any]) -> list[dict[str, Any]]:
        """`POST /designs` with `{ pages: [...] }` — one document per page, in order."""
        payload, url, _ = await self._client._json(ep.create_design(spec))
        return ep.parse_design_pages(payload, url)

    async def render(self, spec: Mapping[str, Any]) -> bytes:
        """`POST /designs/render` — render a spec or document to image/PDF bytes."""
        response = await self._client.send(ep.render_design(spec))
        return response.content
