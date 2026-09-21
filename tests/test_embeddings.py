"""Contract tests for ``appkit.embeddings`` on both backends."""

import json

import httpx
import pytest
import respx

from appkit import AzureOpenAIError, ConfigError, embeddings
from appkit._aoai import DEFAULT_API_VERSION

ENDPOINT = "https://example-aoai.openai.azure.com"
DEPLOYMENT = "text-embedding-3-small"
URL = f"{ENDPOINT}/openai/deployments/{DEPLOYMENT}/embeddings?api-version={DEFAULT_API_VERSION}"


@pytest.fixture
def aoai_env(monkeypatch):
    monkeypatch.setenv("APPKIT_EMBEDDINGS_ENDPOINT", ENDPOINT)
    monkeypatch.delenv("APPKIT_EMBEDDINGS_DEPLOYMENT", raising=False)
    monkeypatch.delenv("APPKIT_EMBEDDINGS_API_VERSION", raising=False)


def _payload(count, *, dims=3, shuffled=False):
    data = [
        {"index": i, "embedding": [float(i)] * dims, "object": "embedding"} for i in range(count)
    ]
    if shuffled:
        data.reverse()
    return {"object": "list", "data": data, "model": DEPLOYMENT}


# --- fake backend ----------------------------------------------------------


def test_fake_is_not_available(fake_backend):
    # An app must be able to tell that these vectors carry no meaning, so it
    # can fall back to lexical search rather than ranking on noise.
    assert embeddings.available() is False


def test_fake_vectors_are_deterministic_and_normalised(fake_backend):
    first = embeddings.embed(["Umfragetool", "Datenbankservice"])
    second = embeddings.embed(["Umfragetool", "Datenbankservice"])

    assert first == second
    assert first[0] != first[1]
    assert len(first[0]) == embeddings.DIMENSIONS
    assert sum(value * value for value in first[0]) == pytest.approx(1.0, rel=1e-6)


def test_empty_input_never_calls_out(fake_backend):
    assert embeddings.embed([]) == []


def test_non_string_input_is_rejected(fake_backend):
    with pytest.raises(TypeError):
        embeddings.embed(["fine", 7])


# --- azure backend ---------------------------------------------------------


def test_available_needs_an_endpoint(azure_backend, monkeypatch):
    monkeypatch.delenv("APPKIT_EMBEDDINGS_ENDPOINT", raising=False)
    assert embeddings.available() is False
    monkeypatch.setenv("APPKIT_EMBEDDINGS_ENDPOINT", ENDPOINT)
    assert embeddings.available() is True


def test_missing_endpoint_raises(azure_backend, monkeypatch):
    monkeypatch.delenv("APPKIT_EMBEDDINGS_ENDPOINT", raising=False)
    with pytest.raises(ConfigError):
        embeddings.embed(["anything"])


@respx.mock
def test_posts_the_inputs_and_returns_vectors(azure_backend, aoai_env):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=_payload(2)))

    vectors = embeddings.embed(["one", "two"])

    assert route.called
    assert json.loads(respx.calls.last.request.content) == {"input": ["one", "two"]}
    assert vectors == [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]


@respx.mock
def test_response_order_follows_the_input(azure_backend, aoai_env):
    # Pairing the wrong vector with the wrong record would show up as bad
    # relevance rather than as an error, so order is restored explicitly.
    respx.post(URL).mock(return_value=httpx.Response(200, json=_payload(3, shuffled=True)))

    vectors = embeddings.embed(["a", "b", "c"])

    assert [vector[0] for vector in vectors] == [0.0, 1.0, 2.0]


@respx.mock
def test_long_input_is_batched(azure_backend, aoai_env, monkeypatch):
    monkeypatch.setattr(embeddings, "BATCH_SIZE", 2)
    sizes = []

    def handler(request):
        batch = json.loads(request.content)["input"]
        sizes.append(len(batch))
        return httpx.Response(200, json=_payload(len(batch)))

    respx.post(URL).mock(side_effect=handler)

    vectors = embeddings.embed(["a", "b", "c", "d", "e"])

    assert sizes == [2, 2, 1]
    assert len(vectors) == 5


@respx.mock
def test_deployment_override(azure_backend, aoai_env):
    url = f"{ENDPOINT}/openai/deployments/other/embeddings?api-version={DEFAULT_API_VERSION}"
    respx.post(url).mock(return_value=httpx.Response(200, json=_payload(1)))

    assert embeddings.embed(["x"], deployment="other")


@respx.mock
def test_throttling_is_retried(azure_backend, aoai_env, monkeypatch):
    monkeypatch.setattr("appkit._aoai._sleep", lambda seconds: None)
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "1"}),
            httpx.Response(200, json=_payload(1)),
        ]
    )

    assert embeddings.embed(["x"]) == [[0.0, 0.0, 0.0]]


@respx.mock
def test_forbidden_explains_the_missing_role(azure_backend, aoai_env):
    respx.post(URL).mock(
        return_value=httpx.Response(
            403, json={"error": {"code": "AuthorizationFailed", "message": "denied"}}
        )
    )

    with pytest.raises(AzureOpenAIError) as caught:
        embeddings.embed(["x"])

    assert caught.value.status == 403
    assert caught.value.code == "AuthorizationFailed"
    assert "Cognitive Services OpenAI User" in str(caught.value)


@respx.mock
def test_unregistered_subscription_is_explained(azure_backend, aoai_env):
    # This is what a caller scoped to the wrong subscription actually gets
    # back, and the message alone does not say what to do about it.
    respx.post(URL).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "code": "SubscriptionNotRegistered",
                    "message": "CheckAccess request is invalid",
                }
            },
        )
    )

    with pytest.raises(AzureOpenAIError) as caught:
        embeddings.embed(["x"])

    assert "az provider register" in str(caught.value)


@respx.mock
def test_unknown_deployment_points_at_the_deployment_name(azure_backend, aoai_env):
    respx.post(URL).mock(
        return_value=httpx.Response(
            404, json={"error": {"code": "DeploymentNotFound", "message": "no such deployment"}}
        )
    )

    with pytest.raises(AzureOpenAIError) as caught:
        embeddings.embed(["x"])

    assert "APPKIT_EMBEDDINGS_DEPLOYMENT" in str(caught.value)


@respx.mock
def test_short_response_is_an_error_not_a_misalignment(azure_backend, aoai_env):
    respx.post(URL).mock(return_value=httpx.Response(200, json=_payload(1)))

    with pytest.raises(AzureOpenAIError) as caught:
        embeddings.embed(["a", "b"])

    assert "expected 2 embeddings" in str(caught.value)


# --- the endpoint is the switch -------------------------------------------


def test_an_endpoint_turns_embeddings_on_even_on_the_fake_backend(fake_backend, monkeypatch):
    """The case this design exists for: fixture data, real vectors.

    Pseudo-vectors cannot show whether a search *ranks* well, so tuning a
    semantic feature otherwise means standing up SharePoint and every other
    integration first, purely to make one of them real.
    """
    monkeypatch.setenv("APPKIT_EMBEDDINGS_ENDPOINT", ENDPOINT)
    monkeypatch.setattr("appkit._credential.token", lambda scope: "test-token")

    assert embeddings.available() is True
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json=_payload(1)))
        assert embeddings.embed(["x"]) == [[0.0, 0.0, 0.0]]


def test_no_endpoint_means_no_network_on_the_fake_backend(fake_backend):
    # No respx mock: reaching the network here would fail the test.
    assert embeddings.available() is False
    assert len(embeddings.embed(["x"])[0]) == embeddings.DIMENSIONS


def test_a_missing_endpoint_in_azure_mode_raises_rather_than_faking(azure_backend, monkeypatch):
    """On a real deployment, no endpoint is a mistake, not a request for noise.

    Returning pseudo-vectors here would surface much later as poor relevance,
    which is far harder to trace back than a raise at startup.
    """
    monkeypatch.delenv("APPKIT_EMBEDDINGS_ENDPOINT", raising=False)
    with pytest.raises(ConfigError, match="APPKIT_EMBEDDINGS_ENDPOINT"):
        embeddings.embed(["x"])


def test_the_suite_is_insulated_from_an_exported_endpoint(fake_backend):
    """`tests/conftest.py` clears it, so a developer's shell cannot make the
    whole suite call Azure OpenAI for real."""
    import os

    assert os.getenv("APPKIT_EMBEDDINGS_ENDPOINT") is None


# --- however the endpoint was written down ---------------------------------


@respx.mock
def test_a_full_deployment_url_is_accepted(azure_backend, monkeypatch):
    """The URL people actually have in front of them is the deployment URL.

    Appending the request path to it yields
    `.../openai/deployments/x/openai/deployments/x/embeddings` and a 404 that
    reads exactly like a genuinely missing deployment — a trap worth removing.
    """
    monkeypatch.setenv(
        "APPKIT_EMBEDDINGS_ENDPOINT",
        f"{ENDPOINT}/openai/deployments/{DEPLOYMENT}",
    )
    monkeypatch.delenv("APPKIT_EMBEDDINGS_DEPLOYMENT", raising=False)
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=_payload(1)))

    assert embeddings.embed(["x"]) == [[0.0, 0.0, 0.0]]
    assert route.called


@respx.mock
def test_a_full_url_supplies_the_deployment_name(azure_backend, monkeypatch):
    monkeypatch.setenv(
        "APPKIT_EMBEDDINGS_ENDPOINT", f"{ENDPOINT}/openai/deployments/text-embedding-3-large"
    )
    monkeypatch.delenv("APPKIT_EMBEDDINGS_DEPLOYMENT", raising=False)
    url = (
        f"{ENDPOINT}/openai/deployments/text-embedding-3-large"
        f"/embeddings?api-version={DEFAULT_API_VERSION}"
    )
    respx.post(url).mock(return_value=httpx.Response(200, json=_payload(1)))

    assert embeddings.embed(["x"])


@respx.mock
def test_an_explicit_deployment_still_wins(azure_backend, monkeypatch):
    monkeypatch.setenv(
        "APPKIT_EMBEDDINGS_ENDPOINT", f"{ENDPOINT}/openai/deployments/from-the-url"
    )
    monkeypatch.setenv("APPKIT_EMBEDDINGS_DEPLOYMENT", "from-the-setting")
    url = (
        f"{ENDPOINT}/openai/deployments/from-the-setting"
        f"/embeddings?api-version={DEFAULT_API_VERSION}"
    )
    respx.post(url).mock(return_value=httpx.Response(200, json=_payload(1)))

    assert embeddings.embed(["x"])


@respx.mock
def test_a_gateway_base_path_is_left_alone(azure_backend, monkeypatch):
    """Only a deployment *path* is trimmed, not a base that ends in /openai."""
    base = "https://gateway.example.ch/openai"
    monkeypatch.setenv("APPKIT_EMBEDDINGS_ENDPOINT", base)
    monkeypatch.delenv("APPKIT_EMBEDDINGS_DEPLOYMENT", raising=False)
    url = f"{base}/openai/deployments/{DEPLOYMENT}/embeddings?api-version={DEFAULT_API_VERSION}"
    respx.post(url).mock(return_value=httpx.Response(200, json=_payload(1)))

    assert embeddings.embed(["x"])


def test_a_trailing_slash_is_harmless(azure_backend, monkeypatch):
    monkeypatch.setenv("APPKIT_EMBEDDINGS_ENDPOINT", ENDPOINT + "/")
    assert embeddings.available() is True


def test_deployment_reports_the_default(fake_backend, monkeypatch):
    """A caller storing vectors can name the model without guessing."""
    monkeypatch.delenv("APPKIT_EMBEDDINGS_DEPLOYMENT", raising=False)
    monkeypatch.delenv("APPKIT_EMBEDDINGS_ENDPOINT", raising=False)

    assert embeddings.deployment() == embeddings.DEFAULT_DEPLOYMENT


def test_deployment_follows_the_same_precedence_as_embed(fake_backend, monkeypatch):
    monkeypatch.setenv(
        "APPKIT_EMBEDDINGS_ENDPOINT",
        "https://x.openai.azure.com/openai/deployments/from-url",
    )
    monkeypatch.delenv("APPKIT_EMBEDDINGS_DEPLOYMENT", raising=False)
    assert embeddings.deployment() == "from-url"

    monkeypatch.setenv("APPKIT_EMBEDDINGS_DEPLOYMENT", "from-env")
    assert embeddings.deployment() == "from-env"

    assert embeddings.deployment("explicit") == "explicit"
