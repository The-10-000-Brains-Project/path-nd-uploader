"""Classifies and retries transient network/GCS errors.

`google-cloud-storage` already retries individual HTTP calls internally —
what we're wrapping here is for when THAT retry budget is exhausted (e.g. a
`RetryError` bubbling up after its own internal 120s timeout) and one more
attempt is worth making before giving up. This exists specifically so a
network hiccup while scanning a file doesn't get reported as a finding
*about that file* — see `is_transient` and how callers use it to route a
failure to an "inconclusive" outcome instead of "corrupted".
"""

from __future__ import annotations

import google.api_core.exceptions as gax_exceptions
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

_TRANSIENT: list[type[BaseException]] = [
    gax_exceptions.RetryError,
    gax_exceptions.ServiceUnavailable,
    gax_exceptions.TooManyRequests,
    gax_exceptions.InternalServerError,
    gax_exceptions.DeadlineExceeded,
    gax_exceptions.GatewayTimeout,
    ConnectionError,
    TimeoutError,
]

# google-cloud-storage runs on `requests` under the hood, and its HTTP-layer
# timeouts (e.g. requests.exceptions.ReadTimeout) subclass neither the
# google.api_core exceptions nor Python's built-in TimeoutError — so without
# this they were misclassified as hard failures. A read timeout mid-transfer
# is transient: retry it, and if it still fails, report it as inconclusive
# (not "corrupted"/"skipped"). Observed for real: 40 objects in a 715-object
# cross-region transfer failed this way and were wrongly marked SKIPPED.
try:
    import requests.exceptions as _req_exc

    _TRANSIENT += [_req_exc.Timeout, _req_exc.ConnectionError, _req_exc.ChunkedEncodingError]
except ImportError:  # pragma: no cover - requests is a google-cloud-storage dependency, always present
    pass

TRANSIENT_EXCEPTIONS = tuple(_TRANSIENT)


def is_transient(exc: BaseException) -> bool:
    return isinstance(exc, TRANSIENT_EXCEPTIONS)


retry_transient = retry(
    retry=retry_if_exception_type(TRANSIENT_EXCEPTIONS),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    reraise=True,  # after exhausting attempts, raise the original exception so callers can still classify it
)
