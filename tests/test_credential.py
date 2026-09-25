"""Whose token each resource gets.

Graph may sign in as an app registration while everything else stays on the
managed identity. The point of the split is Postgres, whose role is the managed
identity -- so the tests below pin both halves, not just the Graph one.
"""

import pytest

from appkit import _credential, doctor
from appkit._credential import GRAPH_APP_VARS, GRAPH_SCOPE
from appkit.db import PG_AAD_SCOPE
from appkit.errors import ConfigError

APP = {
    "APPKIT_GRAPH_CLIENT_ID": "64398970-0000-0000-0000-000000000000",
    "APPKIT_GRAPH_CLIENT_SECRET": "not-a-real-secret",
    "APPKIT_GRAPH_TENANT_ID": "c7e438db-0000-0000-0000-000000000000",
}


class Recorder:
    """Stands in for an azure-identity credential and remembers how it was built."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in GRAPH_APP_VARS:
        monkeypatch.delenv(name, raising=False)
    managed = Recorder()
    monkeypatch.setattr(_credential, "credential", lambda: managed)
    monkeypatch.setattr("azure.identity.ClientSecretCredential", Recorder)
    _credential._graph_app_credential.cache_clear()
    yield managed
    _credential._graph_app_credential.cache_clear()


def _set_app(monkeypatch, **overrides):
    for name, value in {**APP, **overrides}.items():
        monkeypatch.setenv(name, value)


def test_without_graph_vars_everything_uses_the_managed_identity(clean):
    assert _credential.graph_app_client_id() is None
    assert _credential.credential_for(GRAPH_SCOPE) is clean
    assert _credential.credential_for(PG_AAD_SCOPE) is clean


def test_graph_vars_move_graph_and_nothing_else(clean, monkeypatch):
    _set_app(monkeypatch)

    graph = _credential.credential_for(GRAPH_SCOPE)
    assert isinstance(graph, Recorder) and graph is not clean
    assert graph.kwargs == {
        "tenant_id": APP["APPKIT_GRAPH_TENANT_ID"],
        "client_id": APP["APPKIT_GRAPH_CLIENT_ID"],
        "client_secret": APP["APPKIT_GRAPH_CLIENT_SECRET"],
    }
    # The database still signs in as the identity its role belongs to.
    assert _credential.credential_for(PG_AAD_SCOPE) is clean


@pytest.mark.parametrize("missing", GRAPH_APP_VARS)
def test_a_partial_set_is_refused_rather_than_ignored(monkeypatch, missing):
    _set_app(monkeypatch, **{missing: ""})

    with pytest.raises(ConfigError, match=missing):
        _credential.credential_for(GRAPH_SCOPE)


# --- what the doctor says about it ----------------------------------------


@pytest.fixture
def azure(monkeypatch):
    monkeypatch.setenv("APPKIT_BACKEND", "azure")
    monkeypatch.setenv("APPKIT_AUTH", "public")


def _graph_line(checks):
    return next(check for check in checks if check.name == "graph")


def test_startup_names_the_managed_identity_by_default(azure):
    line = _graph_line(doctor.startup_checks())
    assert (line.status, line.detail) == (doctor.PASS, "managed identity")


def test_startup_names_the_app_registration_and_never_the_secret(azure, monkeypatch):
    _set_app(monkeypatch)

    line = _graph_line(doctor.startup_checks())

    assert line.status == doctor.PASS
    assert APP["APPKIT_GRAPH_CLIENT_ID"] in line.detail
    assert APP["APPKIT_GRAPH_CLIENT_SECRET"] not in repr(line)


def test_startup_fails_loudly_on_a_partial_set(azure, monkeypatch):
    _set_app(monkeypatch, APPKIT_GRAPH_TENANT_ID="")

    line = _graph_line(doctor.startup_checks())

    assert line.status == doctor.FAIL
    assert "APPKIT_GRAPH_TENANT_ID" in line.detail


def test_startup_names_graph_even_on_the_fake_backend(monkeypatch):
    """A deployment parked on fake should still say who Graph will be on azure."""
    monkeypatch.setenv("APPKIT_BACKEND", "fake")
    monkeypatch.setenv("APPKIT_AUTH", "dev")
    _set_app(monkeypatch)

    line = _graph_line(doctor.startup_checks())
    assert APP["APPKIT_GRAPH_CLIENT_ID"] in line.detail


def test_credential_check_points_at_the_secret_when_the_registration_fails(
    azure, monkeypatch
):
    _set_app(monkeypatch)

    def refused(scope):
        raise RuntimeError("AADSTS7000215: Invalid client secret provided.")

    monkeypatch.setattr("appkit._credential.token", refused)

    check = doctor.check_credential()

    assert check.status == doctor.FAIL
    assert "APPKIT_GRAPH_CLIENT_SECRET" in check.hint
    assert "managed identity" not in check.hint
