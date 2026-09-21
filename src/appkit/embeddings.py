"""Turn text into vectors with Azure OpenAI.

Public surface::

    from appkit import embeddings

    if embeddings.available():
        vectors = embeddings.embed(["a service description", "another one"])

Embeddings let an app compare text by *meaning* rather than by the words it
happens to contain -- a search for "survey" finding a service that only ever
says "Umfrage". The usual pattern for an internal app is small enough to keep
entirely in memory: embed a few hundred records once, embed the query, and take
the highest cosine similarity. No vector database is involved, and appkit
deliberately does not provide one.

In ``azure`` mode this calls the deployment named by
``APPKIT_EMBEDDINGS_DEPLOYMENT`` on ``APPKIT_EMBEDDINGS_ENDPOINT``, using the
app's managed identity. The identity needs the **Cognitive Services OpenAI
User** role on that resource.

**Configuring an endpoint is what turns embeddings on**, independently of
``APPKIT_BACKEND``. Nobody sets an Azure OpenAI endpoint by accident, so the
variable that says *where* to embed is also the one that says *whether* to —
and that makes one genuinely useful setup ordinary configuration: fixture data
from the fake backend, with vectors that actually mean something, which is what
tuning a search feature needs.

With no endpoint configured, :func:`available` returns ``False`` and
:func:`embed` returns deterministic pseudo-vectors. Those vectors are stable but **meaningless** --
they exist so the surrounding code path can be exercised offline, never so that
relevance can be tested. An app should treat semantic matching as an
enhancement over a lexical search that works without it, and
:func:`available` is how it asks.
"""

from __future__ import annotations

import hashlib
import math
import os
from typing import Any

from .config import env, is_fake

#: Dimensionality of ``text-embedding-3-small``, and of the fake vectors.
DIMENSIONS = 1536

#: Default deployment name. Azure OpenAI deployments are usually named after
#: the model they serve.
DEFAULT_DEPLOYMENT = "text-embedding-3-small"

#: Inputs per request. The service accepts more, but a smaller batch keeps a
#: single failure from costing the whole refresh and stays clear of the
#: per-request token ceiling.
BATCH_SIZE = 128

def available() -> bool:
    """True when :func:`embed` returns vectors that carry meaning.

    ``False`` whenever ``APPKIT_EMBEDDINGS_ENDPOINT`` is unset, which is the
    default everywhere and the normal state locally and in tests. Call this
    before offering a semantic feature and fall back to something lexical when
    it says no -- an app that *requires* embeddings cannot be run or tested
    without an Azure OpenAI resource.
    """
    return bool(_endpoint())


#: Where a deployment URL starts. Everything from here on is built by
#: :func:`_embed_via_azure`, so an endpoint that already contains it was pasted
#: whole rather than trimmed to the resource.
_DEPLOYMENT_PATH = "/openai/"


def _endpoint() -> str:
    """The Azure OpenAI resource endpoint, however it was written down.

    ``APPKIT_EMBEDDINGS_ENDPOINT`` wants the resource
    (``https://x.openai.azure.com``), but the URL people have in front of them
    — from the portal, from a curl example, from a colleague — is usually the
    full deployment URL. Appending the request path to that produces
    ``.../openai/deployments/x/openai/deployments/x/embeddings`` and a 404 that
    looks exactly like a genuinely missing deployment, so both spellings are
    accepted rather than one of them being a trap.

    A gateway whose base path merely *ends* in ``/openai`` is untouched: the
    trim needs the trailing slash that only a deployment path has.
    """
    raw = os.getenv("APPKIT_EMBEDDINGS_ENDPOINT", "").strip().rstrip("/")
    marker = raw.find(_DEPLOYMENT_PATH)
    return raw[:marker] if marker > 0 else raw


def _deployment_from_endpoint() -> str:
    """The deployment named in the endpoint, if it was pasted in whole.

    Saves setting ``APPKIT_EMBEDDINGS_DEPLOYMENT`` to repeat something the URL
    already said. An explicit setting still wins.
    """
    raw = os.getenv("APPKIT_EMBEDDINGS_ENDPOINT", "").strip().rstrip("/")
    _, _, tail = raw.partition("/openai/deployments/")
    return tail.split("/")[0] if tail else ""


def deployment(override: str | None = None) -> str:
    """The embedding deployment :func:`embed` would use.

    Exposed because a caller that *stores* vectors has to record which model
    produced them: vectors from two different deployments are not comparable,
    so a cache that cannot name its deployment cannot tell a stale entry from
    a fresh one.
    """
    return _resolve_deployment(override)


def _resolve_deployment(override: str | None = None) -> str:
    return (
        override
        or os.getenv("APPKIT_EMBEDDINGS_DEPLOYMENT", "").strip()
        or _deployment_from_endpoint()
        or DEFAULT_DEPLOYMENT
    )


def embed(texts: list[str], *, deployment: str | None = None) -> list[list[float]]:
    """Return one vector per input string, in the order given.

    Args:
        texts: The strings to embed. An empty list returns an empty list.
        deployment: Azure OpenAI deployment name. Defaults to
            ``APPKIT_EMBEDDINGS_DEPLOYMENT``, then to
            :data:`DEFAULT_DEPLOYMENT`.

    Raises:
        ConfigError: if ``APPKIT_EMBEDDINGS_ENDPOINT`` is unset while the app
            runs on the ``azure`` backend -- there, a missing endpoint is a
            deployment mistake, not a request for pseudo-vectors.
        AzureOpenAIError: if the service rejects the request.
    """
    if not texts:
        return []
    if any(not isinstance(text, str) for text in texts):
        raise TypeError("embed() takes a list of strings")

    endpoint = _endpoint()
    if not endpoint:
        if is_fake():
            return [_fake_vector(text) for text in texts]
        # On the azure backend a missing endpoint is a misconfigured
        # deployment. Quietly handing back meaningless vectors would surface
        # later as poor relevance, which is far harder to trace than a raise.
        env("APPKIT_EMBEDDINGS_ENDPOINT", required=True)

    deployment = _resolve_deployment(deployment)
    return _embed_via_azure(texts, endpoint=endpoint, deployment=deployment)


def _embed_via_azure(texts: list[str], *, endpoint: str, deployment: str) -> list[list[float]]:
    from . import _aoai

    api_version = (
        os.getenv("APPKIT_EMBEDDINGS_API_VERSION", "").strip() or _aoai.DEFAULT_API_VERSION
    )
    url = f"{endpoint}/openai/deployments/{deployment}/embeddings?api-version={api_version}"

    vectors: list[list[float]] = []
    for start in range(0, len(texts), BATCH_SIZE):
        batch = texts[start : start + BATCH_SIZE]
        payload = _aoai.post(url, {"input": batch})
        vectors.extend(_ordered(payload, expected=len(batch), url=url))
    return vectors


def _ordered(payload: dict[str, Any], *, expected: int, url: str) -> list[list[float]]:
    """Pull the vectors out of a response, restoring the input order.

    The API returns an ``index`` per item and is documented to preserve order,
    but sorting on it explicitly means a future change cannot silently pair the
    wrong vector with the wrong record -- a bug that would look like poor
    relevance rather than like a failure.
    """
    from .errors import AzureOpenAIError

    data = payload.get("data")
    if not isinstance(data, list) or len(data) != expected:
        got = len(data) if isinstance(data, list) else "none"
        raise AzureOpenAIError(
            status=200,
            url=url,
            code="MalformedResponse",
            message=f"expected {expected} embeddings, got {got}",
        )
    ordered = sorted(data, key=lambda item: item.get("index", 0))
    return [list(item["embedding"]) for item in ordered]


def _fake_vector(text: str) -> list[float]:
    """A deterministic unit vector derived from ``text``.

    Derived from a hash, so it is stable across runs and processes -- which is
    what makes tests repeatable -- and carries no semantic information
    whatsoever. Two similar sentences get two unrelated vectors.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values: list[float] = []
    counter = 0
    while len(values) < DIMENSIONS:
        block = hashlib.sha256(digest + counter.to_bytes(4, "big")).digest()
        values.extend((byte - 127.5) / 127.5 for byte in block)
        counter += 1
    values = values[:DIMENSIONS]
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]
