"""Contract tests for ``appkit.chat`` on both backends."""

import json

import httpx
import pytest
import respx

from appkit import AzureOpenAIError, ConfigError, chat
from appkit._aoai import DEFAULT_API_VERSION

ENDPOINT = "https://example-aoai.openai.azure.com"
DEPLOYMENT = "gpt-4o-mini"
URL = (
    f"{ENDPOINT}/openai/deployments/{DEPLOYMENT}/chat/completions"
    f"?api-version={DEFAULT_API_VERSION}"
)


@pytest.fixture
def aoai_env(monkeypatch):
    monkeypatch.setenv("APPKIT_CHAT_ENDPOINT", ENDPOINT)
    monkeypatch.delenv("APPKIT_CHAT_DEPLOYMENT", raising=False)
    monkeypatch.delenv("APPKIT_CHAT_API_VERSION", raising=False)


def _payload(content: str):
    return {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "model": DEPLOYMENT,
    }


# --- fake backend ----------------------------------------------------------


def test_fake_is_not_available(fake_backend):
    assert chat.available() is False


def test_fake_passes_the_prompt_through_unchanged(fake_backend):
    # There is no meaningful stand-in for generated text, so an app doing
    # query expansion should search on the original text, not on noise.
    assert chat.complete("survey tool for staff") == "survey tool for staff"


def test_empty_prompt_never_calls_out(fake_backend):
    assert chat.complete("") == ""


def test_non_string_prompt_is_rejected(fake_backend):
    with pytest.raises(TypeError):
        chat.complete(7)  # type: ignore[arg-type]


# --- azure backend ---------------------------------------------------------


def test_available_needs_an_endpoint(azure_backend, monkeypatch):
    monkeypatch.delenv("APPKIT_CHAT_ENDPOINT", raising=False)
    assert chat.available() is False
    monkeypatch.setenv("APPKIT_CHAT_ENDPOINT", ENDPOINT)
    assert chat.available() is True


def test_missing_endpoint_raises(azure_backend, monkeypatch):
    monkeypatch.delenv("APPKIT_CHAT_ENDPOINT", raising=False)
    with pytest.raises(ConfigError):
        chat.complete("anything")


@respx.mock
def test_posts_the_prompt_and_returns_the_reply(azure_backend, aoai_env):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=_payload("expanded text")))

    result = chat.complete("short demand")

    assert route.called
    body = json.loads(respx.calls.last.request.content)
    assert body == {"messages": [{"role": "user", "content": "short demand"}]}
    assert result == "expanded text"


@respx.mock
def test_reply_is_stripped(azure_backend, aoai_env):
    respx.post(URL).mock(return_value=httpx.Response(200, json=_payload("  padded  \n")))

    assert chat.complete("x") == "padded"


@respx.mock
def test_deployment_override(azure_backend, aoai_env):
    url = f"{ENDPOINT}/openai/deployments/other/chat/completions?api-version={DEFAULT_API_VERSION}"
    respx.post(url).mock(return_value=httpx.Response(200, json=_payload("ok")))

    assert chat.complete("x", deployment="other") == "ok"


@respx.mock
def test_throttling_is_retried(azure_backend, aoai_env, monkeypatch):
    monkeypatch.setattr("appkit._aoai._sleep", lambda seconds: None)
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "1"}),
            httpx.Response(200, json=_payload("ok")),
        ]
    )

    assert chat.complete("x") == "ok"


@respx.mock
def test_forbidden_explains_the_missing_role(azure_backend, aoai_env):
    respx.post(URL).mock(
        return_value=httpx.Response(
            403, json={"error": {"code": "AuthorizationFailed", "message": "denied"}}
        )
    )

    with pytest.raises(AzureOpenAIError) as caught:
        chat.complete("x")

    assert caught.value.status == 403
    assert "Cognitive Services OpenAI User" in str(caught.value)


@respx.mock
def test_no_choices_is_a_malformed_response(azure_backend, aoai_env):
    respx.post(URL).mock(return_value=httpx.Response(200, json={"choices": []}))

    with pytest.raises(AzureOpenAIError, match="no choices"):
        chat.complete("x")


@respx.mock
def test_missing_message_content_is_a_malformed_response(azure_backend, aoai_env):
    respx.post(URL).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"role": "assistant"}}]})
    )

    with pytest.raises(AzureOpenAIError, match="no message content"):
        chat.complete("x")


# --- the endpoint is the switch, independently of embeddings ---------------


def test_no_endpoint_means_no_network_on_the_fake_backend(fake_backend):
    # No respx mock: reaching the network here would fail the test.
    assert chat.available() is False
    assert chat.complete("x") == "x"


def test_a_missing_endpoint_in_azure_mode_raises_rather_than_passing_through(
    azure_backend, monkeypatch
):
    monkeypatch.delenv("APPKIT_CHAT_ENDPOINT", raising=False)
    with pytest.raises(ConfigError, match="APPKIT_CHAT_ENDPOINT"):
        chat.complete("x")


@respx.mock
def test_chat_and_embeddings_endpoints_are_independent(azure_backend, monkeypatch):
    """An environment can serve chat and embeddings off different resources."""
    from appkit import embeddings

    monkeypatch.setenv("APPKIT_CHAT_ENDPOINT", ENDPOINT)
    monkeypatch.delenv("APPKIT_EMBEDDINGS_ENDPOINT", raising=False)

    assert chat.available() is True
    assert embeddings.available() is False


def test_a_trailing_slash_is_harmless(azure_backend, monkeypatch):
    monkeypatch.setenv("APPKIT_CHAT_ENDPOINT", ENDPOINT + "/")
    assert chat.available() is True
