"""snapnedit — the official Python client for the snapnedit AI photo-editing api.

```python
from snapnedit import Snapnedit

snap = Snapnedit(api_key="sk_live_…")
result = snap.run("remove-background", "cat.jpg")
open("cat-cutout.png", "wb").write(result.output or b"")
```

Everything a caller needs is exported here: the sync :class:`Snapnedit` and
async :class:`AsyncSnapnedit` clients, the typed views they return, the
bring-your-own-storage input/destination types, :class:`SnapneditError` with
the closed :class:`ErrorCode` set, and the :class:`Webhooks` verifier.

Developed in the snapnedit monorepo and mirrored to
https://github.com/Snap-N-Edit/sdk-python. API reference:
https://snapnedit.com/docs/api-reference
"""

from ._inputs import ImageInput
from ._transport import DEFAULT_BASE_URL, RetryPolicy
from ._version import __version__
from .async_client import (
    AsyncDesignsClient,
    AsyncDestinationsClient,
    AsyncEmbedClient,
    AsyncSnapnedit,
    AsyncWebhookEndpointsClient,
)
from .client import (
    DesignsClient,
    DestinationsClient,
    EmbedClient,
    Snapnedit,
    WebhookEndpointsClient,
)
from .errors import (
    ERROR_CODES,
    ErrorCode,
    SnapneditError,
    SnapneditSignatureError,
    SnapneditTimeoutError,
)
from .models import (
    UNSET,
    USAGE_UNATTRIBUTED,
    Account,
    AccountKey,
    CreateJobResult,
    Destination,
    DestinationPresign,
    DestinationTestResult,
    EmbedToken,
    JobDelivery,
    JobDestinationSummary,
    JobEnvelope,
    JobInput,
    JobListItem,
    JobPriority,
    JobState,
    JobStatus,
    JobView,
    OperationMetadata,
    PresignedPutDestination,
    RunResult,
    SavedDestination,
    SignedUrl,
    StorageDestination,
    StorageDestinationTest,
    UnsetType,
    UploadResult,
    UrlInput,
    UsageGroupBy,
    UsageInstant,
    UsageKeyRow,
    UsageRange,
    UsageReport,
    UsageSeriesPoint,
    UsageSource,
    UsageTotals,
    WebhookEndpoint,
    WebhookEndpointCreated,
    WebhookEndpointEvent,
)
from .operations import (
    CREDIT_COSTS,
    MASK_OPERATIONS,
    OPERATION_IDS,
    OperationId,
    credit_cost,
    requires_mask,
)
from .webhooks import (
    WEBHOOK_DELIVERY_HEADER,
    WEBHOOK_EVENT_HEADER,
    WEBHOOK_SIGNATURE_HEADER,
    WebhookEvent,
    WebhookJobData,
    Webhooks,
    WebhookSessionData,
)

__all__ = [
    "CREDIT_COSTS",
    "DEFAULT_BASE_URL",
    "ERROR_CODES",
    "MASK_OPERATIONS",
    "OPERATION_IDS",
    "UNSET",
    "USAGE_UNATTRIBUTED",
    "WEBHOOK_DELIVERY_HEADER",
    "WEBHOOK_EVENT_HEADER",
    "WEBHOOK_SIGNATURE_HEADER",
    "Account",
    "AccountKey",
    "AsyncDesignsClient",
    "AsyncDestinationsClient",
    "AsyncEmbedClient",
    "AsyncSnapnedit",
    "AsyncWebhookEndpointsClient",
    "CreateJobResult",
    "DesignsClient",
    "Destination",
    "DestinationPresign",
    "DestinationTestResult",
    "DestinationsClient",
    "EmbedClient",
    "EmbedToken",
    "ErrorCode",
    "ImageInput",
    "JobDelivery",
    "JobDestinationSummary",
    "JobEnvelope",
    "JobInput",
    "JobListItem",
    "JobPriority",
    "JobState",
    "JobStatus",
    "JobView",
    "OperationId",
    "OperationMetadata",
    "PresignedPutDestination",
    "RetryPolicy",
    "RunResult",
    "SavedDestination",
    "SignedUrl",
    "Snapnedit",
    "SnapneditError",
    "SnapneditSignatureError",
    "SnapneditTimeoutError",
    "StorageDestination",
    "StorageDestinationTest",
    "UnsetType",
    "UploadResult",
    "UrlInput",
    "UsageGroupBy",
    "UsageInstant",
    "UsageKeyRow",
    "UsageRange",
    "UsageReport",
    "UsageSeriesPoint",
    "UsageSource",
    "UsageTotals",
    "WebhookEndpoint",
    "WebhookEndpointCreated",
    "WebhookEndpointEvent",
    "WebhookEndpointsClient",
    "WebhookEvent",
    "WebhookJobData",
    "WebhookSessionData",
    "Webhooks",
    "__version__",
    "credit_cost",
    "requires_mask",
]
