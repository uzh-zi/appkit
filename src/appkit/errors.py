"""Exceptions raised by appkit.

Application code that wants to react to a failure (rather than let it become a
500) catches these. Everything appkit raises deliberately derives from
:class:`AppkitError`::

    from appkit import GraphError, sharepoint

    try:
        rows = sharepoint.list_rows("Requests")
    except GraphError as exc:
        if exc.status == 403:
            ...   # the app's identity is missing a Graph permission
"""

from __future__ import annotations


class AppkitError(RuntimeError):
    """Base class for every error appkit raises on purpose."""


class ConfigError(AppkitError):
    """The environment is not configured the way the active backend needs."""


class GraphError(AppkitError):
    """A Microsoft Graph request failed.

    Carries the pieces you actually need to diagnose it: the HTTP status, the
    Graph error ``code`` and ``message`` from the response body, and the
    ``request-id`` header (this is the value Microsoft support asks for).
    """

    def __init__(
        self,
        *,
        status: int,
        method: str,
        url: str,
        code: str = "",
        message: str = "",
        request_id: str = "",
    ) -> None:
        self.status = status
        self.method = method
        self.url = url
        self.code = code
        self.message = message
        self.request_id = request_id
        super().__init__(self._describe())

    def _describe(self) -> str:
        detail = " - ".join(part for part in (self.code, self.message) if part)
        text = f"Graph {self.method} {self.url} failed: {self.status}"
        if detail:
            text += f" ({detail})"
        if self.request_id:
            text += f" [request-id: {self.request_id}]"
        if hint := _hint(self.status):
            text += f"\n{hint}"
        return text


def _hint(status: int) -> str:
    if status in (401, 403):
        return (
            "Hint: the app's managed identity is probably missing a Graph "
            "permission (Sites.Read.All for SharePoint, Mail.Send for mail), or "
            "admin consent for it has not been granted."
        )
    if status == 404:
        return (
            "Hint: check APPKIT_SHAREPOINT_SITE and the list key. Graph resolves "
            "a list by its id or its URL name, not by its display name."
        )
    if status == 429:
        return "Hint: Graph is throttling this app; appkit already retried."
    return ""


class AzureOpenAIError(AppkitError):
    """An Azure OpenAI request failed.

    Same idea as :class:`GraphError`: keep the status, the service's own error
    ``code`` and ``message``, and the request id, because the response body is
    what says whether this was a missing role assignment, an unknown deployment
    or a content filter.
    """

    def __init__(
        self,
        *,
        status: int,
        url: str,
        code: str = "",
        message: str = "",
        request_id: str = "",
    ) -> None:
        self.status = status
        self.url = url
        self.code = code
        self.message = message
        self.request_id = request_id
        super().__init__(self._describe())

    def _describe(self) -> str:
        detail = " - ".join(part for part in (self.code, self.message) if part)
        text = f"Azure OpenAI POST {self.url} failed: {self.status}"
        if detail:
            text += f" ({detail})"
        if self.request_id:
            text += f" [request-id: {self.request_id}]"
        if hint := _aoai_hint(self.status, self.code, self.url, self.message):
            text += f"\n{hint}"
        return text


def _aoai_hint(status: int, code: str = "", url: str = "", message: str = "") -> str:
    if code == "SubscriptionNotRegistered":
        return (
            "Hint: the subscription the caller is scoped to has not registered the "
            "Microsoft.CognitiveServices resource provider. Run "
            "`az provider register --namespace Microsoft.CognitiveServices` on it, "
            "or point the identity at the subscription that owns the resource."
        )
    if status in (401, 403):
        # A resource behind a VNet or firewall rejects an outside caller with a
        # 403 that has nothing to do with RBAC. Pointing at the role assignment
        # there sends people to re-grant a role they already have.
        if "virtual network" in message.lower() or "firewall" in message.lower():
            return (
                "Hint: this is a network restriction, not a missing role. The "
                "Azure OpenAI resource only accepts callers from an allowed "
                "VNet or IP range, so a developer machine needs the VPN or an "
                "allowlist entry; a deployed app needs to egress from the "
                "allowed network."
            )
        return (
            "Hint: the app's managed identity is probably missing the 'Cognitive "
            "Services OpenAI User' role on the Azure OpenAI resource, or the token "
            "was issued for the wrong audience."
        )
    if status == 404:
        setting = "APPKIT_CHAT" if "/chat/completions" in url else "APPKIT_EMBEDDINGS"
        return (
            f"Hint: check {setting}_ENDPOINT and "
            f"{setting}_DEPLOYMENT. Azure OpenAI resolves a model by its "
            "*deployment* name, which need not match the model name. A model "
            "newer than the pinned API version can also 404 here -- try "
            f"{setting}_API_VERSION before assuming the name is wrong."
        )
    if status == 429:
        return "Hint: the deployment is rate-limited; appkit already retried."
    return ""
