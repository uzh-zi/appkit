"""Expand short text into better search terms with Azure OpenAI chat.

Public surface::

    from appkit import chat

    if chat.available():
        expanded = chat.complete(prompt)

This exists for the same reason :mod:`appkit.embeddings` does: an app that
matches free text against a catalog gets much better relevance from a query
that has been expanded into fuller, more descriptive language first. appkit
does not attempt anything beyond that single-turn job -- no conversation
history, no streaming, no function calling.

In ``azure`` mode this calls the deployment named by
``APPKIT_CHAT_DEPLOYMENT`` on ``APPKIT_CHAT_ENDPOINT``, using the app's
managed identity. The identity needs the **Cognitive Services OpenAI User**
role on that resource -- the same role :mod:`appkit.embeddings` needs, so an
app that already has embeddings working on a resource needs no new grant to
also use chat there.

**Configuring an endpoint is what turns chat on**, independently of
``APPKIT_BACKEND`` and independently of ``APPKIT_EMBEDDINGS_ENDPOINT`` -- an
environment could plausibly serve the two off different resources, and an app
should be able to enable one without the other.

With no endpoint configured, :func:`available` returns ``False`` and
:func:`complete` returns ``prompt`` unchanged. Unlike the fake embedding
vectors, there is no meaningful "meaningless but deterministic" stand-in for
generated text -- passing the prompt straight through is exactly what an app
doing query expansion wants when expansion is unavailable: search on the
original text rather than on nothing.
"""

from __future__ import annotations

import os

from .config import env, is_fake

#: Default deployment name. Azure OpenAI deployments are usually named after
#: the model they serve.
DEFAULT_DEPLOYMENT = "gpt-4o-mini"


def available() -> bool:
    """True when :func:`complete` calls a real model.

    ``False`` whenever ``APPKIT_CHAT_ENDPOINT`` is unset, which is the default
    everywhere and the normal state locally and in tests. Call this before
    relying on the expansion and fall back to the original text when it says
    no -- an app that *requires* chat cannot be run or tested without an Azure
    OpenAI resource.
    """
    return bool(_endpoint())


#: Where a deployment URL starts, mirroring ``appkit.embeddings``.
_DEPLOYMENT_PATH = "/openai/"


def _endpoint() -> str:
    """The Azure OpenAI resource endpoint, however it was written down.

    See :func:`appkit.embeddings._endpoint` -- the same acceptance of a full
    deployment URL applies here.
    """
    raw = os.getenv("APPKIT_CHAT_ENDPOINT", "").strip().rstrip("/")
    marker = raw.find(_DEPLOYMENT_PATH)
    return raw[:marker] if marker > 0 else raw


def _deployment_from_endpoint() -> str:
    """The deployment named in the endpoint, if it was pasted in whole."""
    raw = os.getenv("APPKIT_CHAT_ENDPOINT", "").strip().rstrip("/")
    _, _, tail = raw.partition("/openai/deployments/")
    return tail.split("/")[0] if tail else ""


def complete(prompt: str, *, deployment: str | None = None) -> str:
    """Return a single completion for ``prompt``.

    Args:
        prompt: The user message to send. An empty prompt returns an empty
            string without contacting anything.
        deployment: Azure OpenAI deployment name. Defaults to
            ``APPKIT_CHAT_DEPLOYMENT``, then to :data:`DEFAULT_DEPLOYMENT`.

    Returns:
        The assistant's reply text, stripped of surrounding whitespace. When
        chat is unavailable (no endpoint configured, or the fake backend with
        no endpoint), returns ``prompt`` unchanged.

    Raises:
        ConfigError: if ``APPKIT_CHAT_ENDPOINT`` is unset while the app runs
            on the ``azure`` backend -- there, a missing endpoint is a
            deployment mistake, not a request to skip expansion.
        AzureOpenAIError: if the service rejects the request.
    """
    if not isinstance(prompt, str):
        raise TypeError("complete() takes a string")
    if not prompt:
        return ""

    endpoint = _endpoint()
    if not endpoint:
        if is_fake():
            return prompt
        # On the azure backend a missing endpoint is a misconfigured
        # deployment, not a request for a passthrough -- the same reasoning
        # as appkit.embeddings.embed().
        env("APPKIT_CHAT_ENDPOINT", required=True)

    deployment = (
        deployment
        or os.getenv("APPKIT_CHAT_DEPLOYMENT", "").strip()
        or _deployment_from_endpoint()
        or DEFAULT_DEPLOYMENT
    )
    return _complete_via_azure(prompt, endpoint=endpoint, deployment=deployment)


def _complete_via_azure(prompt: str, *, endpoint: str, deployment: str) -> str:
    from . import _aoai

    api_version = os.getenv("APPKIT_CHAT_API_VERSION", "").strip() or _aoai.DEFAULT_API_VERSION
    url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"

    payload = {"messages": [{"role": "user", "content": prompt}]}
    response = _aoai.post(url, payload)
    return _message(response, url=url).strip()


def _message(payload: dict, *, url: str) -> str:
    """Pull the assistant's reply out of a chat-completions response."""
    from .errors import AzureOpenAIError

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AzureOpenAIError(
            status=200, url=url, code="MalformedResponse", message="no choices in response"
        )
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise AzureOpenAIError(
            status=200,
            url=url,
            code="MalformedResponse",
            message="choice had no message content",
        )
    return content
