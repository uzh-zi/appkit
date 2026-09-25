"""Thin Microsoft Graph client used by the Azure-backed modules.

This is the *only* place in appkit that imports ``httpx`` and speaks HTTP to
Graph. Application code must never do this directly – it calls
:func:`appkit.sharepoint.list_rows` or :func:`appkit.mail.send_mail` instead.

All requests authenticate with the app's managed identity, or with an app
registration when ``APPKIT_GRAPH_*`` names one (see :mod:`appkit._credential`),
and go through :func:`_send`, which:

* retries throttling and transient server errors with backoff, honouring
  Graph's ``Retry-After`` header;
* turns any remaining failure into a :class:`~appkit.errors.GraphError` that
  carries the Graph error code, message and ``request-id`` – the response body
  says *why* a call was rejected, and losing it makes a production permission
  problem nearly undiagnosable.

Retries are chosen per HTTP method. ``sendMail`` is not idempotent, so a POST is
only retried when we know the request was *not* processed (429 throttling, 503
service unavailable, or a failure to connect at all). A GET may be retried more
freely.
"""

from __future__ import annotations

import time
from typing import Any

from ._credential import GRAPH_SCOPE
from .errors import GraphError

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_TIMEOUT = 30.0

_MAX_ATTEMPTS = 4
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 60.0

# Safe to retry anywhere: the server told us it did not process the request.
_RETRY_ALWAYS = frozenset({429, 503})
# Additionally retried for reads, where repeating the request is harmless.
_RETRY_READ = _RETRY_ALWAYS | frozenset({500, 502, 504})

_sleep = time.sleep  # module attribute so tests can neutralise the backoff


def _headers() -> dict[str, str]:
    from ._credential import token

    return {
        "Authorization": f"Bearer {token(GRAPH_SCOPE)}",
        "Accept": "application/json",
    }


def _url(path: str) -> str:
    return path if path.startswith("http") else f"{GRAPH_BASE}{path}"


def _retry_statuses(method: str) -> frozenset[int]:
    return _RETRY_READ if method == "GET" else _RETRY_ALWAYS


def _retry_after(response: Any, attempt: int) -> float:
    """Seconds to wait: Graph's ``Retry-After`` if usable, else exponential."""
    raw = response.headers.get("retry-after", "")
    try:
        return min(float(raw), _BACKOFF_CAP)
    except (TypeError, ValueError):
        # Retry-After may also be an HTTP-date; fall back to plain backoff.
        return min(_BACKOFF_BASE * (2**attempt), _BACKOFF_CAP)


def _fail(response: Any, method: str, url: str) -> GraphError:
    """Build a GraphError that keeps everything the response body told us."""
    code = message = ""
    try:
        error = response.json().get("error", {})
        if isinstance(error, dict):
            code = str(error.get("code", ""))
            message = str(error.get("message", ""))
    except ValueError:
        message = (response.text or "")[:500]

    return GraphError(
        status=response.status_code,
        method=method,
        url=url,
        code=code,
        message=message,
        request_id=response.headers.get("request-id")
        or response.headers.get("client-request-id")
        or "",
    )


def _send(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    """Perform one Graph request with retries; return the httpx response."""
    import httpx

    retryable = _retry_statuses(method)
    last_connect_error: Exception | None = None

    with httpx.Client(timeout=_TIMEOUT) as client:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = client.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers={**_headers(), **(headers or {})},
                )
            except httpx.ConnectError as exc:
                # The request never reached Graph, so repeating it is safe even
                # for a POST.
                last_connect_error = exc
                if attempt + 1 == _MAX_ATTEMPTS:
                    raise
                _sleep(min(_BACKOFF_BASE * (2**attempt), _BACKOFF_CAP))
                continue

            if response.status_code in retryable and attempt + 1 < _MAX_ATTEMPTS:
                _sleep(_retry_after(response, attempt))
                continue

            if response.status_code >= 400:
                raise _fail(response, method, url)
            return response

    raise last_connect_error or RuntimeError("unreachable")


def get(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """GET ``path`` (relative to the Graph base) and return parsed JSON."""
    return _send("GET", _url(path), params=params, headers=headers).json()


def get_all(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """GET a collection, following ``@odata.nextLink`` pagination.

    ``headers`` are sent on every page, so an advanced-query header such as
    ``ConsistencyLevel: eventual`` survives pagination.
    """
    items: list[dict[str, Any]] = []
    url: str | None = _url(path)
    query = params
    while url:
        payload = _send("GET", url, params=query, headers=headers).json()
        items.extend(payload.get("value", []))
        url = payload.get("@odata.nextLink")
        query = None  # nextLink already carries the query
    return items


def post(path: str, json: dict[str, Any]) -> None:
    """POST ``json`` to ``path``; raise :class:`GraphError` on failure."""
    _send("POST", _url(path), json=json)
