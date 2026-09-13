"""The synchronous client: `Snapnedit`."""

from __future__ import annotations

import os
import time
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
    UsageGroupBy,
    UsageInstant,
    UsageReport,
    UsageSource,
)

__all__ = ["Snapnedit"]

USER_AGENT = f"snapnedit-python/{__version__}"

DEFAULT_POLL_INTERVAL = 1.0
DEFAULT_JOB_TIMEOUT = 120.0


class Snapnedit:
    """A synchronous snapnedit api client.

    ```python
    from snapnedit import Snapnedit

    with Snapnedit(api_key="sk_live_…") as snap:
        result = snap.run("remove-background", "cat.jpg")
        assert result.output is not None
        open("cat-cutout.png", "wb").write(result.output)
    ```

    Every method raises :class:`~snapnedit.SnapneditError` for a non-2xx
    response or a job that finished in `failed`; branch on `err.code`, never on
    `err.message`.
    """

    destinations: DestinationsClient
    """Saved storage destinations: list, create, update, delete, test, presign_upload."""
    embed: EmbedClient
    """Embed tokens: `create_session` (publishable key) and `create_token` (api key)."""
    designs: DesignsClient
    """Declarative designs: `create`, `create_pages`, `render`."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        timeout: float = 60.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
        http_client: httpx.Client | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Build a client.

        Args:
            api_key: A secret `sk_…` key or an embed token. Defaults to
                `$SNAPNEDIT_API_KEY`; `None` leaves calls anonymous.
            base_url: Api origin. Defaults to `$SNAPNEDIT_BASE_URL`, then
                `https://snapnedit.com/api`.
            timeout: Per-request HTTP timeout in seconds.
            max_retries: Extra attempts for idempotent calls that hit a 429,
                a 5xx or a network error. Exponential backoff with jitter,
                honoring `Retry-After`.
            transport: An `httpx` transport (a `MockTransport` in tests).
            http_client: Bring your own `httpx.Client`; it is not closed for you.
            user_agent: Overrides the default `snapnedit-python/<version>`.
        """
        self._api_key = api_key if api_key is not None else os.environ.get("SNAPNEDIT_API_KEY")
        self._base_url = (
            base_url or os.environ.get("SNAPNEDIT_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self._retry = RetryPolicy(max_retries=max(max_retries, 0))
        self._user_agent = user_agent or USER_AGENT
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout, transport=transport)
        self.destinations = DestinationsClient(self)
        self.embed = EmbedClient(self)
        self.designs = DesignsClient(self)

    # -- plumbing ----------------------------------------------------------

    @property
    def base_url(self) -> str:
        """The api origin this client talks to."""
        return self._base_url

    def with_token(self, token: str) -> Snapnedit:
        """Build a second client for the same api, authenticating with `token` instead.

        Shares this client's underlying `httpx.Client` (so closing either one
        closes the connection pool once). Handy for an embed token minted by
        :meth:`EmbedClient.create_token`.
        """
        return Snapnedit(
            token,
            self._base_url,
            max_retries=self._retry.max_retries,
            http_client=self._http,
            user_agent=self._user_agent,
        )

    def close(self) -> None:
        """Close the underlying connection pool (unless you supplied the client)."""
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Snapnedit:
        """Enter a `with` block."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the client on the way out of a `with` block."""
        self.close()

    def _headers(self, request: Request) -> dict[str, str]:
        headers = {key.lower(): value for key, value in request.headers.items()}
        headers.setdefault("user-agent", self._user_agent)
        headers.setdefault("accept", "application/json")
        if request.auth and self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        return headers

    def send(self, request: Request) -> httpx.Response:
        """Send one prepared :class:`Request`, retrying and raising per the shared policy."""
        url = resolve_url(self._base_url, request)
        headers = self._headers(request)
        attempt = 0
        while True:
            try:
                response = self._http.request(
                    request.method,
                    url,
                    headers=headers,
                    content=request.content,
                    json=request.json_body if request.content is None else None,
                )
            except httpx.HTTPError as exc:
                if request.retryable and attempt < self._retry.max_retries:
                    attempt += 1
                    time.sleep(self._retry.delay(attempt))
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
                time.sleep(
                    self._retry.delay(attempt, retry_after_seconds(dict(response.headers)))
                )
                continue
            check_response(response.status_code, response.content, url)
            return response

    def _json(self, request: Request) -> tuple[Any, str, int]:
        response = self.send(request)
        url = str(response.request.url)
        return parse_json(response.content, response.status_code, url), url, response.status_code

    # -- catalog -----------------------------------------------------------

    def list_operations(self) -> list[OperationMetadata]:
        """`GET /operations` — every operation, its accepted mimes and params schema."""
        payload, url, _ = self._json(ep.list_operations())
        return ep.parse_operations(payload, url)

    # -- uploads -----------------------------------------------------------

    def upload(self, image: ImageInput, mime: str | None = None) -> UploadResult:
        """Upload one asset: `POST /uploads` -> presigned `PUT` -> `POST /confirm`.

        The confirm step is not optional — until it runs the asset carries a
        placeholder hash and cannot take part in content-based caching.
        """
        data, resolved_mime = read_image(image, mime)
        payload, url, _ = self._json(ep.create_upload(resolved_mime, len(data)))
        asset_id, signed = ep.parse_create_upload(payload, url)
        self.send(ep.put_upload(signed.url, data, resolved_mime))
        payload, url, _ = self._json(ep.confirm_upload(asset_id))
        confirmed_id, content_hash, size = ep.parse_confirm_upload(payload, url)
        return UploadResult(asset_id=confirmed_id, content_hash=content_hash, bytes=size)

    # -- jobs --------------------------------------------------------------

    def create_job(
        self,
        operation: str,
        input: str | UrlInput | Mapping[str, Any],
        params: Mapping[str, Any] | None = None,
        *,
        destination: Destination | UnsetType | None = UNSET,
    ) -> CreateJobResult:
        """`POST /jobs`.

        Args:
            operation: An operation id (see :data:`snapnedit.OPERATION_IDS`).
            input: An uploaded asset id, or a :class:`UrlInput` the SERVER fetches.
            params: The operation's own params, validated strictly server-side.
            destination: Where to deliver the result. Omit to apply your
                account default; pass `None` to opt this job out of it.

        Returns a :class:`CreateJobResult` whose `status` may already be
        `succeeded` — `result.cached` is `True` when the api answered a free
        cache hit.
        """
        url_input = as_url_input(input)
        job_input: str | UrlInput = url_input if url_input is not None else str(input)
        payload, url, status = self._json(
            ep.create_job(operation, job_input, params, destination)
        )
        return ep.parse_create_job(payload, url, status)

    def get_job(self, job_id: str) -> JobView:
        """`GET /jobs/{id}` — one poll. Someone else's job id is a 404, never a 403."""
        payload, url, _ = self._json(ep.get_job(job_id))
        return ep.parse_job(payload, url, job_id)

    def wait_for_job(
        self,
        job_id: str,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        timeout: float = DEFAULT_JOB_TIMEOUT,
    ) -> JobView:
        """Poll until the job is settled — terminal AND any delivery resolved.

        Raises :class:`~snapnedit.SnapneditTimeoutError` if that takes longer
        than `timeout`. Does NOT raise for a job that failed; read
        `view.error_code` (or use :meth:`run`, which does raise).
        """
        deadline = Deadline.start(timeout)
        while True:
            view = self.get_job(job_id)
            if view.settled:
                return view
            time.sleep(deadline.sleep_for(poll_interval, job_id))

    def run(
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
        """Upload, run, poll and download in one call.

        Args:
            operation: An operation id.
            input: Bytes, a path, an open binary file, or a :class:`UrlInput`
                for an image the server fetches itself (no upload at all).
            params: The operation's params.
            mask: A second image for a mask-guided operation
                (`magic-eraser`, `generative-fill`, `remove-watermark`).
                Uploaded and passed as `params["maskAssetId"]`.
            mime: Mime of `input`, when it cannot be sniffed.
            destination: Deliver the result straight into your bucket. Omit to
                apply your account default; `None` opts out.
            download: Force the result bytes to be fetched (`True`) or skipped
                (`False`). By default they are fetched unless you named a
                destination and the delivery succeeded.
            poll_interval: Seconds between polls.
            timeout: Total polling budget in seconds.

        Raises:
            SnapneditError: the api refused the call, or the job failed.
            SnapneditTimeoutError: the job never settled within `timeout`.
        """
        url_input = as_url_input(input)
        job_input: str | UrlInput
        if url_input is not None:
            job_input = url_input
        else:
            data, resolved_mime = read_image(input, mime)  # type: ignore[arg-type]
            job_input = self.upload(data, resolved_mime).asset_id

        job_params = dict(params or {})
        if mask is not None:
            mask_data, mask_mime = read_image(mask)
            job_params["maskAssetId"] = self.upload(mask_data, mask_mime).asset_id

        created = self.create_job(operation, job_input, job_params, destination=destination)
        view = created.as_view()
        if not view.settled:
            view = self.wait_for_job(created.job_id, poll_interval=poll_interval, timeout=timeout)

        failure = job_failure(view)
        if failure is not None:
            raise failure

        if should_download(download, destination, view) and view.download is not None:
            output, output_mime = self._download(view.download)
            return RunResult(
                job_id=view.job_id,
                downloaded=True,
                input=view.input,
                destination=view.destination,
                delivery=view.delivery,
                download=view.download,
                output=output,
                mime=output_mime,
                credit_cost=view.credit_cost,
                cached=view.cached,
            )
        return RunResult(
            job_id=view.job_id,
            downloaded=False,
            input=view.input,
            destination=view.destination,
            delivery=view.delivery,
            download=view.download,
            credit_cost=view.credit_cost,
            cached=view.cached,
        )

    def download_result(self, source: JobView | RunResult | SignedUrl | str) -> bytes:
        """Fetch result bytes from a job, a run result or a presigned url."""
        signed: SignedUrl | str | None = (
            source.download if isinstance(source, (JobView, RunResult)) else source
        )
        if signed is None:
            raise SnapneditError(
                ErrorCode.NOT_FOUND,
                "there is no downloadable copy of this result (delivered and deleted)",
            )
        return self._download(signed)[0]

    def _download(self, signed: SignedUrl | str) -> tuple[bytes, str]:
        response = self.send(ep.download(signed))
        return response.content, response.headers.get("content-type", "application/octet-stream")

    # -- usage -------------------------------------------------------------

    def get_usage(
        self,
        *,
        start: UsageInstant | None = None,
        end: UsageInstant | None = None,
        group_by: UsageGroupBy | None = None,
        key_id: str | None = None,
        origin: str | None = None,
        operation: str | None = None,
        source: UsageSource | None = None,
    ) -> UsageReport:
        """`GET /usage` — jobs, credits and embed sessions over a date range.

        Args:
            start: Inclusive window start (the wire's `from`). An ISO string, a
                `date` (the whole UTC day) or a `datetime`. Defaults to 30 days
                before `end`.
            end: Inclusive window end (the wire's `to`). Defaults to now. A
                window wider than 366 days is refused with `invalid_input`.
            group_by: How `series` is bucketed — `"day"` (the default, zero-filled
                and oldest first), `"key"`, `"origin"`, `"operation"` or
                `"source"`. Everything but `day` comes back busiest first.
            key_id: Only jobs authenticated with this api key.
            origin: Only embed jobs from this host surface.
            operation: Only jobs for this operation id.
            source: Only jobs that arrived this way.

        Readable with a secret key or an embed token. An embed token is scoped
        to its own key — `key_id` is ignored for it and `report.keys` is empty.
        """
        payload, url, _ = self._json(
            ep.get_usage(
                start=start,
                end=end,
                group_by=group_by,
                key_id=key_id,
                origin=origin,
                operation=operation,
                source=source,
            )
        )
        return ep.parse_usage(payload, url)


class DestinationsClient:
    """Saved storage destinations — `client.destinations`."""

    def __init__(self, client: Snapnedit) -> None:
        """Bind to its parent client."""
        self._client = client

    def list(self) -> list[StorageDestination]:
        """`GET /destinations` — credential-free views of every saved bucket."""
        payload, url, _ = self._client._json(ep.list_destinations())
        return ep.parse_destination_list(payload, url)

    def create(
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
        """`POST /destinations` — save a bucket. The secret key is never echoed back.

        Max 10 per account. `is_default=True` makes every subsequent api-key or
        embed job deliver here unless it passes `destination=None`.
        """
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
        payload, url, _ = self._client._json(ep.create_destination(body))
        return ep.parse_destination(payload, url)

    def update(
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
        """`PATCH /destinations/{id}`.

        `access_key_id` and `secret_access_key` must move together. `provider`
        is not patchable — delete and recreate.
        """
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
        payload, url, _ = self._client._json(ep.update_destination(destination_id, body))
        return ep.parse_destination(payload, url)

    def delete(self, destination_id: str) -> None:
        """`DELETE /destinations/{id}` — 204. Deleting your default leaves you without one."""
        self._client.send(ep.delete_destination(destination_id))

    def test(self, destination_id: str) -> DestinationTestResult:
        """`POST /destinations/{id}/test` — a real write-then-delete probe.

        Always resolves: the HTTP call succeeded either way, and `ok` says
        whether the bucket did.
        """
        payload, url, _ = self._client._json(ep.test_destination(destination_id))
        return ep.parse_destination_test(payload, url)

    def presign_upload(
        self, destination_id: str, *, ext: str, content_type: str
    ) -> DestinationPresign:
        """`POST /destinations/{id}/presign` — a 15-minute PUT into your own bucket.

        `content_type` must be the canonical type for `ext`, and the returned
        `headers` must be sent verbatim: the signature covers them.
        """
        payload, url, _ = self._client._json(
            ep.presign_destination_upload(destination_id, ext, content_type)
        )
        return ep.parse_destination_presign(payload, url)


class EmbedClient:
    """Embedded-editor credentials — `client.embed`."""

    def __init__(self, client: Snapnedit) -> None:
        """Bind to its parent client."""
        self._client = client

    def create_session(self, publishable_key: str, host_origin: str) -> EmbedToken:
        """`POST /embed/sessions` — exchange a `pk_` key for a short-lived token.

        Sent without an `Authorization` header: the publishable key is the
        credential, and `host_origin` must be one of the key's allowed origins
        (or a `native:<bundle-id>` shell) — anything else is a `403 forbidden`.
        """
        payload, url, _ = self._client._json(
            ep.create_embed_session(publishable_key, host_origin)
        )
        return ep.parse_embed_token(payload, url)

    def create_token(
        self,
        *,
        ttl_seconds: int | UnsetType = UNSET,
        end_user_id: str | UnsetType = UNSET,
        max_credits: int | UnsetType = UNSET,
        allowed_operations: Sequence[str] | UnsetType = UNSET,
        origin: str | UnsetType = UNSET,
    ) -> EmbedToken:
        """`POST /embed/tokens` — mint a scoped embed token from your own server.

        Requires the account's secret key. The token authenticates like one:
        `Snapnedit(token.token, base_url)` (or :meth:`Snapnedit.with_token`)
        can then run jobs, billed to the account.
        """
        payload, url, _ = self._client._json(
            ep.create_embed_token(
                ttl_seconds=ttl_seconds,
                end_user_id=end_user_id,
                max_credits=max_credits,
                allowed_operations=allowed_operations,
                origin=origin,
            )
        )
        return ep.parse_embed_token(payload, url)


class DesignsClient:
    """Declarative designs — `client.designs`."""

    def __init__(self, client: Snapnedit) -> None:
        """Bind to its parent client."""
        self._client = client

    def create(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        """`POST /designs` — compile a design spec into one editor document."""
        payload, url, _ = self._client._json(ep.create_design(spec))
        return ep.parse_design(payload, url)

    def create_pages(self, spec: Mapping[str, Any]) -> list[dict[str, Any]]:
        """`POST /designs` with `{ pages: [...] }` — one document per page, in order."""
        payload, url, _ = self._client._json(ep.create_design(spec))
        return ep.parse_design_pages(payload, url)

    def render(self, spec: Mapping[str, Any]) -> bytes:
        """`POST /designs/render` — render a spec or document to image/PDF bytes."""
        response = self._client.send(ep.render_design(spec))
        return response.content
