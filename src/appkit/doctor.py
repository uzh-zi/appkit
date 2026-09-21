"""Check that a deployment is wired up the way it thinks it is.

Run it in the container, as the app::

    python -m appkit.doctor
    python -m appkit.doctor --json                    # for a Container Apps Job
    python -m appkit.doctor --list Requests --send-mail you@uzh.ch

For a report an app can log on every boot, without contacting anything, see
:func:`log_startup`.

Every check runs independently and reports what it found, so one broken thing
does not hide the rest. Nothing is contacted on the ``fake`` backend, and no
token, password or connection string is ever printed.

It exists because the failures that matter here are quiet ones: the wrong
backend discards mail while returning success, a managed identity missing a
Graph role fails only on the code path nobody exercised, and a SharePoint list
resolves by a different name in Azure than it does locally. The doctor asks
each of those questions out loud.

Exit code is 0 if nothing failed (warnings and skips are fine), 1 otherwise.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import platform
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

from . import config

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"


@dataclass
class Check:
    """One question the doctor asked, and what came back."""

    name: str
    status: str
    detail: str
    notes: list[str] = field(default_factory=list)
    hint: str = ""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _token_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying it.

    This is diagnostics, not authentication: the token was just handed to us by
    our own credential, and we only want to report what it says about itself.
    """
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return {}


class _CredentialNameCapture(logging.Handler):
    """Catch which credential in the chain actually answered.

    azure-identity logs this and nothing else exposes it, so the capture is
    best-effort: an empty result just means the report is one line shorter.
    """

    PATTERN = re.compile(r"acquired a token from (\w+)")

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.name_found = ""

    def emit(self, record: logging.LogRecord) -> None:
        match = self.PATTERN.search(record.getMessage())
        if match:
            self.name_found = match.group(1)

    def __enter__(self) -> _CredentialNameCapture:
        self._logger = logging.getLogger("azure.identity")
        self._previous = self._logger.level
        self._logger.setLevel(logging.INFO)
        self._logger.addHandler(self)
        return self

    def __exit__(self, *exc: object) -> None:
        self._logger.removeHandler(self)
        self._logger.setLevel(self._previous)


def _redact(value: str, keep: int = 6) -> str:
    if not value:
        return "(unset)"
    return value if len(value) <= keep else f"{value[:keep]}…"


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------

def check_environment() -> Check:
    notes = [f"python {platform.python_version()} on {sys.platform}"]
    if config.on_azure_platform():
        import os

        marker = next(
            (m for m in ("CONTAINER_APP_NAME", "WEBSITE_SITE_NAME") if os.getenv(m)), ""
        )
        detail = f"running on an Azure app platform ({marker}={os.getenv(marker)})"
    else:
        detail = "not on an Azure app platform"
    return Check("environment", PASS, detail, notes)


def check_backend() -> Check:
    try:
        active = config.backend()
    except Exception as exc:
        return Check("backend", FAIL, str(exc))

    if active == config.AZURE:
        return Check("backend", PASS, "azure")

    if config.on_azure_platform():
        return Check(
            "backend",
            WARN,
            "fake, on an Azure app platform",
            hint="Mail is discarded, database writes vanish on restart and "
            "SharePoint returns seed data — all while looking like they worked. "
            "Set APPKIT_BACKEND=azure unless this is deliberate.",
        )
    return Check("backend", PASS, "fake (in-memory; nothing will be contacted)")


def check_auth() -> Check:
    from . import auth

    try:
        active = auth.mode()
    except Exception as exc:
        return Check("auth", FAIL, str(exc))

    if active != auth.VERIFY:
        if active == auth.EASYAUTH:
            note = (
                "Trusting X-MS-CLIENT-PRINCIPAL headers. This is only safe if "
                "Easy Auth rejects unauthenticated requests."
            )
        elif active == auth.PUBLIC:
            note = (
                "No user is ever established and the platform headers are "
                "ignored, so anyone who can reach this app can use it."
            )
        else:
            note = "Local dev user; refused on an Azure app platform."
        return Check("auth", PASS, active, [note])

    missing = [
        name
        for name in ("APPKIT_AUTH_TENANT_ID", "APPKIT_AUTH_CLIENT_ID")
        if not config.env(name)
    ]
    if missing:
        return Check("auth", FAIL, f"verify, but {', '.join(missing)} not set")
    try:
        import jwt  # noqa: F401
    except ImportError:
        return Check(
            "auth", FAIL, "verify, but PyJWT is not installed",
            hint="Install the extra: uv pip install 'appkit[verify]'",
        )
    return Check("auth", PASS, "verify (id-token signature is checked)")


def check_credential() -> Check:
    from ._credential import token
    from ._graph import GRAPH_SCOPE

    try:
        with _CredentialNameCapture() as capture:
            raw = token(GRAPH_SCOPE)
    except Exception as exc:
        return Check(
            "credential", FAIL, f"{type(exc).__name__}: {exc}",
            hint="No managed identity is available. In Azure, check the identity "
            "is assigned to the app; locally, run `az login`.",
        )

    claims = _token_claims(raw)
    roles = claims.get("roles") or []
    scopes = str(claims.get("scp", "")).split()

    if roles:
        kind = "application (app-only) token"
    elif scopes:
        kind = "delegated token (a user is behind it)"
    else:
        kind = "token acquired (could not read its claims)"

    detail = f"{capture.name_found} -> {kind}" if capture.name_found else kind
    # These two get mixed up constantly: the site grant takes the client id, the
    # Graph app-role assignment takes the object id.
    notes = [
        f"client id (appid) = {claims.get('appid') or claims.get('azp') or '?'}",
        f"object id (oid)   = {claims.get('oid', '?')}",
        f"tenant     (tid)  = {claims.get('tid', '?')}",
    ]
    if roles:
        notes.append(f"Graph app roles: {', '.join(sorted(roles))}")
    elif scopes:
        notes.append(f"delegated scopes: {', '.join(sorted(scopes))}")

    check = Check("credential", PASS, detail, notes)
    if scopes and not roles:
        check.hint = (
            "A delegated token proves the Graph request shapes but not the "
            "app-only permission model. Production uses a managed identity, "
            "which gets `roles` instead of `scp`."
        )
    if not roles and not scopes:
        check.status = WARN
    return check


def check_sharepoint(list_name: str | None) -> Check:
    from . import _graph

    site = config.env("APPKIT_SHAREPOINT_SITE")
    if not site:
        return Check("sharepoint", SKIP, "APPKIT_SHAREPOINT_SITE not set")

    try:
        info = _graph.get(f"/sites/{site}")
    except Exception as exc:
        return Check("sharepoint", FAIL, f"cannot read the site: {exc}")

    notes = []
    try:
        lists = _graph.get_all(f"/sites/{site}/lists", params={"$top": 50})
    except Exception as exc:
        return Check(
            "sharepoint", FAIL, f"site resolves but its lists do not: {exc}",
            notes=[f"site: {info.get('displayName', '?')}"],
        )

    # Graph resolves /lists/{key} by id or `name`, never by display name, so
    # report all three — this is the mismatch that breaks apps built on fakes.
    notes.append("pass one of `name` or `id` to list_rows(), not the display name:")
    for item in lists[:20]:
        notes.append(
            f"  name={item.get('name', '?')!r:<28} "
            f"display={item.get('displayName', '?')!r:<28} id={item.get('id', '?')}"
        )
    if len(lists) > 20:
        notes.append(f"  … and {len(lists) - 20} more")

    detail = f"site {info.get('displayName', '?')!r}; {len(lists)} lists visible"

    if list_name:
        from . import sharepoint

        try:
            rows = sharepoint.list_rows(list_name)
        except Exception as exc:
            return Check(
                "sharepoint", FAIL, f"{detail}; reading {list_name!r} failed: {exc}",
                notes=notes,
            )
        detail += f"; {list_name!r} returned {len(rows)} rows"

    return Check("sharepoint", PASS, detail, notes)


def check_mail(send_to: str | None) -> Check:
    sender = config.env("APPKIT_MAIL_SENDER")
    if not sender:
        return Check("mail", SKIP, "APPKIT_MAIL_SENDER not set")

    if not send_to:
        return Check(
            "mail", SKIP, f"sender is {sender}; not verified",
            hint="Pass --send-mail ADDRESS to actually send one and prove "
            "Mail.Send works for this mailbox.",
        )

    from . import mail

    try:
        mail.send_mail(
            to=send_to,
            subject="appkit doctor",
            body="This message was sent by `python -m appkit.doctor`.",
        )
    except Exception as exc:
        return Check(
            "mail", FAIL, f"sending as {sender} failed: {exc}",
            hint="Mail.Send may be missing, or an Exchange ApplicationAccessPolicy "
            "may exclude this mailbox.",
        )
    return Check("mail", PASS, f"sent as {sender} to {send_to}")


def check_database() -> Check:
    dsn = config.env("APPKIT_DB_DSN")
    if not dsn:
        return Check("database", SKIP, "APPKIT_DB_DSN not set")

    from . import db

    try:
        row = db.query("select version() as version, current_user as who")[0]
    except Exception as exc:
        return Check(
            "database", FAIL, f"{type(exc).__name__}: {exc}",
            hint="Check the DSN host and that this identity has an AAD role on "
            "the server. The password is a token appkit fetches per connection.",
        )

    version = str(row.get("version", "")).split(",")[0]
    return Check("database", PASS, version, [f"connected as {row.get('who', '?')}"])


def check_directory() -> Check:
    """One real people search, to prove User.Read.All is actually granted.

    Opt-in: a directory search is a query about real people, so it happens only
    when someone names a term to search for.
    """
    term = (config.env("APPKIT_DIRECTORY_PROBE") or "").strip()
    if not term:
        return Check(
            "directory", SKIP, "APPKIT_DIRECTORY_PROBE not set",
            hint="Set it to a name fragment to prove User.Read.All is granted "
            "and that onPremisesSamAccountName comes back.",
        )

    from . import directory

    try:
        people = directory.search_people(term, limit=1)
    except Exception as exc:
        return Check(
            "directory", FAIL, f"{type(exc).__name__}: {exc}",
            hint="User.Read.All may be missing, or admin consent may not have "
            "been granted for it.",
        )
    if not people:
        return Check(
            "directory", WARN, f"nobody matched {term!r}",
            hint="The call succeeded, so the permission is fine -- but pick a "
            "term that matches somebody to prove the fields come back.",
        )
    person = people[0]
    detail = f"found {person.display_name}"
    if not person.shortname:
        return Check(
            "directory", FAIL, f"{detail}, but with no shortname",
            hint="onPremisesSamAccountName came back empty. Apps that authorize "
            "per person key on it, and email is not a substitute.",
        )
    return Check("directory", PASS, detail, [f"shortname {person.shortname}"])


def check_dns() -> Check:
    """Resolve a name that must exist, so a broken resolver cannot look like
    "the name is free".

    Opt-in, so neither a test run nor a boot report reaches the network by
    surprise.
    """
    probe = (config.env("APPKIT_DNS_PROBE") or "").strip()
    if not probe:
        return Check(
            "dns", SKIP, "APPKIT_DNS_PROBE not set",
            hint="Set it to a name that must resolve. A resolver that answers "
            "nothing would report every name as free.",
        )

    from . import dns as dns_module  # noqa: F811

    try:
        records = dns_module.resolve(probe, "A")
    except Exception as exc:
        return Check(
            "dns", FAIL, f"{type(exc).__name__}: {exc}",
            hint="Check APPKIT_DNS_SERVER and that egress to it is allowed.",
        )
    if not records:
        return Check(
            "dns", FAIL, f"{probe} did not resolve",
            hint="An availability check against this resolver would report every "
            "name as free.",
        )
    return Check("dns", PASS, f"{probe} resolves", records[:2])


#: What switches each integration on, and what it does. The startup report
#: reads these; the contacting checks above use the same names.
_INTEGRATIONS = (
    ("sharepoint", "APPKIT_SHAREPOINT_SITE", "read SharePoint lists"),
    ("mail", "APPKIT_MAIL_SENDER", "send mail"),
    ("database", "APPKIT_DB_DSN", "query Postgres"),
    ("embeddings", "APPKIT_EMBEDDINGS_ENDPOINT", "embed text"),
    ("chat", "APPKIT_CHAT_ENDPOINT", "complete chat prompts"),
)

#: Settings whose value must never be printed. A DSN carries a host *and*
#: often a user; the rest are hostnames and mailbox addresses, which are the
#: whole point of the report.
_SECRET = frozenset({"APPKIT_DB_DSN"})


def settings_checks() -> list[Check]:
    """One check per integration: configured, or not, and what that means.

    Nothing is contacted — this only reads configuration, so it is cheap
    enough to run on every boot. "Not configured" is reported as SKIP rather
    than passed over in silence, because an integration that is quietly absent
    is the thing people lose an afternoon to.
    """
    checks: list[Check] = []
    fake = config.backend() == config.FAKE

    for name, variable, purpose in _INTEGRATIONS:
        value = (config.env(variable) or "").strip()

        if name == "sharepoint" and fake:
            directory = (config.env("APPKIT_SHAREPOINT_FAKE_DIR") or "").strip()
            if directory:
                checks.append(Check(name, PASS, f"exports in {directory}"))
            else:
                checks.append(
                    Check(name, SKIP, "APPKIT_SHAREPOINT_FAKE_DIR not set",
                          hint="Lists will be the built-in seed data.")
                )
            continue

        if not value:
            checks.append(Check(name, SKIP, f"{variable} not set; cannot {purpose}"))
        elif variable in _SECRET:
            checks.append(Check(name, PASS, "configured"))
        else:
            checks.append(Check(name, PASS, value))

    return checks


def startup_checks() -> list[Check]:
    """Everything that can be reported without contacting anything.

    Meant to be logged as an app starts. The quiet failures appkit exists to
    prevent — the wrong backend discarding mail, an integration nobody
    configured — are invisible until someone notices the results are wrong, so
    an app should say out loud what it is about to do. For the questions that
    need a network round trip (does this identity *really* have Mail.Send?),
    run the full doctor.
    """
    checks = [check_environment(), check_backend()]
    if checks[-1].status == FAIL:
        # Everything below reads the backend, so it would only repeat this.
        return checks
    checks.append(check_auth())
    checks.extend(settings_checks())
    return checks


def log_startup(logger: logging.Logger | None = None, *, level: int = logging.INFO):
    """Write :func:`startup_checks` to ``logger``, one line each.

    Returns the checks, so a caller can add its own lines or react to a FAIL.

    Note that a web server usually configures only its own loggers and leaves
    the root one alone, so ``logging.basicConfig()`` (or equivalent) has to
    have run or these records go nowhere.
    """
    logger = logger or logging.getLogger("appkit")
    checks = startup_checks()
    for check in checks:
        logger.log(level, "config | %-12s %-5s %s", check.name, check.status, check.detail)
        if check.hint:
            logger.log(level, "config | %-12s       %s", "", check.hint)
    return checks


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

def run(*, list_name: str | None = None, send_to: str | None = None) -> list[Check]:
    """Run every check and return the results in report order."""
    checks = [check_environment(), check_backend()]

    if checks[-1].status == FAIL:
        # Everything below reads the backend, so it would only repeat this error.
        return checks

    checks.append(check_auth())

    if config.is_fake():
        checks.append(
            Check("integrations", SKIP, "fake backend: nothing was contacted")
        )
        return checks

    checks.append(check_credential())
    checks.append(check_sharepoint(list_name))
    checks.append(check_mail(send_to))
    checks.append(check_database())
    checks.append(check_directory())
    checks.append(check_dns())
    return checks


def format_report(checks: list[Check]) -> str:
    lines = ["appkit doctor", "=" * 13, ""]
    for check in checks:
        lines.append(f"{check.status:<5} {check.name:<12} {check.detail}")
        for note in check.notes:
            lines.append(f"{'':<18} {note}")
        for index, chunk in enumerate(_wrap(check.hint, 78) if check.hint else []):
            lines.append(f"{'':<18} {'->' if index == 0 else '  '} {chunk}")
    failed = [c.name for c in checks if c.status == FAIL]
    lines.append("")
    lines.append(f"{len(failed)} failed" if failed else "all checks passed")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words, line, out = text.split(), "", []
    for word in words:
        if line and len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m appkit.doctor",
        description="Check that this deployment is wired up the way it thinks it is.",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    parser.add_argument("--list", dest="list_name", help="also read rows from this list")
    parser.add_argument(
        "--send-mail", dest="send_to", help="actually send a test mail to this address"
    )
    args = parser.parse_args(argv)

    checks = run(list_name=args.list_name, send_to=args.send_to)

    if args.json:
        print(json.dumps({"checks": [asdict(c) for c in checks]}, indent=2))
    else:
        print(format_report(checks))

    return 1 if any(c.status == FAIL for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
