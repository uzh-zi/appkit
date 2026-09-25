"""Credentials, shared across the Azure-backed modules.

Everything that talks to Azure (Graph, Postgres, Azure OpenAI) authenticates as
the app's **managed identity** by default. In production that is the
user-assigned or system-assigned identity bound to the Azure Container App;
locally (when you deliberately run with ``APPKIT_BACKEND=azure``)
``DefaultAzureCredential`` also picks up the Azure CLI / VS Code / environment
credentials, which is handy for debugging.

**Graph may sign in as an app registration instead.** Graph application
permissions such as ``Sites.Selected`` are granted by the Entra team, and at UZH
they grant them to app registrations, not to managed identities. So when
``APPKIT_GRAPH_CLIENT_ID``, ``APPKIT_GRAPH_CLIENT_SECRET`` and
``APPKIT_GRAPH_TENANT_ID`` are all set, Graph tokens come from that registration
and everything else stays on the managed identity. It is per resource on
purpose: an app's Postgres role is its managed identity, so swapping the
process-wide credential (e.g. via ``AZURE_CLIENT_SECRET``) would sign it into
the database as a principal the database does not know.

The import of :mod:`azure.identity` is deferred so that the default ``fake``
backend never needs the Azure SDK installed at runtime.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import TYPE_CHECKING

from .errors import ConfigError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from azure.core.credentials import TokenCredential

GRAPH_SCOPE = "https://graph.microsoft.com/.default"

#: All three or none: a partial set is a deployment mistake, and falling back to
#: the managed identity would hide it behind a 401 from Graph.
GRAPH_APP_VARS = (
    "APPKIT_GRAPH_CLIENT_ID",
    "APPKIT_GRAPH_CLIENT_SECRET",
    "APPKIT_GRAPH_TENANT_ID",
)


@lru_cache(maxsize=1)
def credential() -> TokenCredential:
    """Return a process-wide :class:`DefaultAzureCredential`."""
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


def graph_app_client_id() -> str | None:
    """The app registration Graph signs in as, or ``None`` for the managed identity.

    Raises:
        ConfigError: if only some of :data:`GRAPH_APP_VARS` are set.
    """
    values = {name: os.getenv(name, "").strip() for name in GRAPH_APP_VARS}
    missing = [name for name, value in values.items() if not value]
    if len(missing) == len(GRAPH_APP_VARS):
        return None
    if missing:
        raise ConfigError(
            f"{', '.join(missing)} not set, but the other APPKIT_GRAPH_* variables "
            "are. Set all three to sign in to Graph as an app registration, or none "
            "to use the managed identity."
        )
    return values["APPKIT_GRAPH_CLIENT_ID"]


@lru_cache(maxsize=1)
def _graph_app_credential() -> TokenCredential:
    from azure.identity import ClientSecretCredential

    return ClientSecretCredential(
        tenant_id=os.environ["APPKIT_GRAPH_TENANT_ID"].strip(),
        client_id=os.environ["APPKIT_GRAPH_CLIENT_ID"].strip(),
        client_secret=os.environ["APPKIT_GRAPH_CLIENT_SECRET"].strip(),
    )


def credential_for(scope: str) -> TokenCredential:
    """The credential that should answer for ``scope``."""
    if scope == GRAPH_SCOPE and graph_app_client_id():
        return _graph_app_credential()
    return credential()


def token(scope: str) -> str:
    """Fetch a bearer token for ``scope`` (see the module docstring for whose)."""
    return credential_for(scope).get_token(scope).token
