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
)
from .client import DesignsClient, DestinationsClient, EmbedClient, Snapnedit
from .errors import (
    ERROR_CODES,
    ErrorCode,
    SnapneditError,
    SnapneditSignatureError,
    SnapneditTimeoutError,
)
from .models import (
    UNSET,
    CreateJobResult,
    Destination,
    DestinationPresign,
    DestinationTestResult,
    EmbedToken,
    JobDelivery,
    JobDestinationSummary,
    JobEnvelope,
    JobInput,
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
)

__all__ = [
    "CREDIT_COSTS",
    "DEFAULT_BASE_URL",
    "ERROR_CODES",
    "MASK_OPERATIONS",
    "OPERATION_IDS",
    "UNSET",
    "WEBHOOK_DELIVERY_HEADER",
    "WEBHOOK_EVENT_HEADER",
    "WEBHOOK_SIGNATURE_HEADER",
    "AsyncDesignsClient",
    "AsyncDestinationsClient",
    "AsyncEmbedClient",
    "AsyncSnapnedit",
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
    "WebhookEvent",
    "WebhookJobData",
    "Webhooks",
    "__version__",
    "credit_cost",
    "requires_mask",
]
