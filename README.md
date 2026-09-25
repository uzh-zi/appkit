# appkit

The UZH internal-app toolkit. Small modules, one job each — so that a
business app (and the AI assistant writing it) never has to touch Microsoft
Graph, `httpx`, or `psycopg` by hand.

Every module authenticates with the app's **managed identity** in production
(Graph can use an app registration instead, see below) and ships with an
**in-memory fake** for local development and tests. No secrets, no connection
strings in code, no network in the test suite.

```python
from appkit import sharepoint, mail, auth, db, directory

rows = sharepoint.list_rows("Requests")            # Graph list items -> dicts
mail.send_mail(to="team@uzh.ch", subject="Summary", body="...")   # Graph sendMail
who = auth.user(request)                            # Easy Auth headers -> User
people = directory.search_people("hartmann")        # Graph /users -> Person
db.execute("insert into note (body) values (%s)", ["hi"])         # psycopg pool
```

## The two backends

appkit runs in one of two backends, chosen by the `APPKIT_BACKEND` environment
variable:

| `APPKIT_BACKEND` | Behaviour |
| --- | --- |
| `fake` *(default locally)* | Pure Python, in-memory. No Azure credentials, no network. Used by local dev and the whole test suite. |
| `azure` | Talks to Microsoft Graph and Azure Postgres using `DefaultAzureCredential` (the managed identity). |

The backend is chosen **strictly**. An unrecognised value is an error, and an
*unset* value is an error whenever the app is running on Azure Container Apps or
App Service (detected via `CONTAINER_APP_NAME` / `WEBSITE_SITE_NAME`). Off
platform, unset still means `fake`.

That strictness exists because every fake failure looks like a success:
`send_mail()` returns normally without sending anything, `db.execute()` writes to
an in-memory database that disappears on the next restart, `list_rows()` renders
seed data as if it were real, and `auth.user()` hands back an *authenticated*
dev user carrying whatever roles `APPKIT_DEV_ROLES` names. A production app with
a dropped or misspelled variable must fail loudly, not quietly serve fiction.

## Modules

### `appkit.sharepoint`

```python
sharepoint.list_rows("Requests")                       # all items as field dicts
sharepoint.list_rows("Requests", select=["Title"])     # project fields
sharepoint.list_rows("Requests", site="contoso.sharepoint.com,<siteId>,<webId>")
```

Reads list items from Graph (`/sites/{site}/lists/{list}/items?expand=fields`),
following pagination. The site defaults to `APPKIT_SHAREPOINT_SITE`.

**Real data in the fake backend.** Rather than hand-writing seed rows, open the
list in SharePoint and use **Export → Export to Excel** or **Export to CSV**.
Save the file as `<ListName>.xlsx` / `<ListName>.csv` in a folder of your choice:

```sh
export APPKIT_SHAREPOINT_FAKE_DIR=./sharepoint-exports
# ./sharepoint-exports/Requests.csv  ->  sharepoint.list_rows("Requests")
```

Every export in that folder becomes the list named after its file, so
application code is unchanged. Rows are normalised to look like what Graph
returns: the export's `ID` column becomes the string `id` key (rows are numbered
`"1"`, `"2"`, … if there is no such column) and empty cells are dropped rather
than returned as `None`. Lists without a matching file keep their built-in seed,
and exports are re-read on every `reset_fakes()`, so they stay in place across
tests.

The **CSV export carries the list's structure**, and that is what makes it the
more faithful of the two. It opens with a `ListSchema=` preamble holding the
field definitions, followed by a header row of the list's *internal* field names
— the same keys Graph puts in an item's `fields` facet. The schema is used for
field types, so a multi-choice column comes back as a real list:

```python
sharepoint.list_rows("Services")[0]
# {"id": "10", "Title": "Confluence", "user": ["Mitarbeitende"],
#  "kosten": "kostenlos", ...}
```

Multi-line `Note` fields keep their newlines and HTML entities are unescaped.
Everything other than multi-choice comes back as text: an export carries no
type information beyond the schema, so values are not guessed at. (The Excel
export has no schema at all — its header is display names, and only dates get
special treatment, becoming ISO strings.)

A single file can also be loaded directly, which is handy in a fixture:

```python
from appkit import _fake
_fake.load_sharepoint_export("tests/data/Requests.csv", list_name="Requests")
```

Misconfiguration is loud rather than silent: a missing directory, an unreadable
export, or two files claiming the same list all raise.
> **Naming a list.** The fake backend looks lists up by display name; Graph
> resolves `/lists/{key}` by the list's **id** or **URL name**. A display name
> with a space in it therefore works locally and returns 404 in Azure. Use the
> list id, or a URL name without spaces.

### `appkit.mail`

```python
mail.send_mail(to="team@uzh.ch", subject="Hi", body="Text")
mail.send_mail(to=["a@uzh.ch", "b@uzh.ch"], cc="boss@uzh.ch",
               subject="Report", body="<p>HTML</p>", html=True)
mail.outbox()   # fake backend: inspect what was "sent"
```

Sends via `POST /users/{sender}/sendMail`. The sender defaults to
`APPKIT_MAIL_SENDER`.

### `appkit.directory`

```python
from appkit import directory

people = directory.search_people("hartmann, nicole")     # most relevant first
staff  = directory.search_people("mei", employees_only=True)
person = directory.person_by_shortname("nhartma")
person = directory.person_by_id(user.id)                 # by Entra object id
```

Each `Person` carries `shortname`, `display_name`, `email`, `id` and
`employee_type`. **`shortname` — Entra's `onPremisesSamAccountName` — is the
identity.** Email is not: shared mailboxes mean two people can hold the same
address, so an app that authorizes on email hands one of them the other's
permissions. The fake directory contains such a pair on purpose, so that
mistake fails a test rather than reaching production.

Results are ranked here, not by the server: an exact match on a leading word
beats a prefix, which beats a mid-word substring, and per-token tiers are
summed so an entry matching every token prominently sorts first. Searching
`sandra` therefore offers *Sandra Meier* before *Alessandra Rossi* without
dropping the latter.

One backend difference worth knowing: Graph's `$filter` offers `startsWith`,
not arbitrary substring matching, so `azure` finds people by a **prefix** of
their display name, given name, surname, mail or shortname — searching
`essandra` will not find *Alessandra*. The fake matches substrings too, so it
is the more forgiving of the two. Needs Graph `User.Read.All`.

### `appkit.dns`

```python
from appkit import dns

dns.cname_exists("myapp.azr.uzh.ch")     # True if the name is already taken
dns.resolve("myapp.azr.uzh.ch", "A")     # the records, or []
```

For telling someone a hostname is taken before they ask for it. A name that
does not exist is `False`; a lookup that could not be *completed* raises
`DnsLookupError`, because reporting an unreachable resolver as "the name is
free" is how two things end up with the same hostname.

Unlike the other modules, `azure` here is not an Azure service — it is a real
resolver (against `APPKIT_DNS_SERVER` when set). The fake answers from an
in-memory set of names.

### `appkit.embeddings`

```python
from appkit import embeddings

if embeddings.available():
    vectors = embeddings.embed(["a service description", "another one"])
```

Turns text into vectors with Azure OpenAI, so an app can match on *meaning*
rather than on the words a record happens to contain — a search for "survey"
finding a service that only ever says "Umfrage".

For the sizes internal apps deal in, keep the whole thing in memory: embed a few
hundred records once, embed the query, take the highest cosine similarity. There
is no vector database here and appkit does not provide one.

**`available()` is the important call.** On the fake backend it returns `False`
and `embed()` hands back deterministic pseudo-vectors — stable, so tests repeat,
and meaningless, so nothing about relevance can be concluded from them. Build the
feature as an enhancement over a lexical search that works without embeddings,
and ask `available()` before blending them in. An app that *requires* embeddings
cannot be run or tested locally.

The identity needs the **Cognitive Services OpenAI User** role on the resource.
Inputs are sent in batches of `BATCH_SIZE`, and the response is re-sorted by its
`index` field so a vector can never be paired with the wrong record.

**The endpoint is the switch.** `APPKIT_EMBEDDINGS_ENDPOINT` decides both
*where* to embed and *whether* to, independently of `APPKIT_BACKEND` — nobody
sets an Azure OpenAI endpoint by accident. That makes one genuinely useful setup
ordinary configuration: fixture data with real vectors, which is what tuning a
search feature needs.

```sh
# list data from APPKIT_SHAREPOINT_FAKE_DIR, real vectors from Azure OpenAI
export APPKIT_BACKEND=fake
export APPKIT_EMBEDDINGS_ENDPOINT=https://<resource>.openai.azure.com
```

Either spelling of the endpoint works — the resource
(`https://x.openai.azure.com`) or the full deployment URL you are more likely to
have in front of you (`https://x.openai.azure.com/openai/deployments/my-model`).
Appending the request path to the second form would otherwise produce a 404 that
reads exactly like a genuinely missing deployment. The full form also supplies
the deployment name, so `APPKIT_EMBEDDINGS_DEPLOYMENT` only needs setting when
it differs.

Unset it and embeddings go back to pseudo-vectors — which is also how you turn
semantic search off without redeploying anything else. `tests/conftest.py`
clears it, so an endpoint exported in a developer's shell cannot quietly make a
test suite call out.

The one asymmetry: on the **azure** backend a missing endpoint raises instead of
faking. There, no endpoint is a deployment mistake, and handing back meaningless
vectors would surface later as poor relevance — much harder to trace than a
raise.

### `appkit.chat`

```python
if chat.available():
    query = chat.complete(f"Rewrite this demand as a search query: {demand_text}")
else:
    query = demand_text
```

A single-turn text completion, for the same job embeddings are usually paired
with: turning a short or jargon-heavy demand into fuller text before embedding
and searching it. No conversation history, no streaming, no function calling —
just a prompt in, a reply out.

It follows `appkit.embeddings`' shape exactly: `APPKIT_CHAT_ENDPOINT` is the
switch, independent of both `APPKIT_BACKEND` and
`APPKIT_EMBEDDINGS_ENDPOINT` (an environment can serve the two off different
resources), the identity needs the same **Cognitive Services OpenAI User**
role, and either spelling of the endpoint (resource root or full deployment
URL) is accepted.

The one difference from embeddings: there is no meaningful "meaningless but
deterministic" stand-in for generated text, so on the fake backend — or on
`azure` with no endpoint configured — `complete()` returns the prompt
unchanged rather than inventing one. That is exactly what an app doing query
expansion wants when expansion is unavailable: search on the original text,
not on nothing. As with embeddings, a missing endpoint on the `azure` backend
still raises rather than silently falling back — there, it is a deployment
mistake.

### `appkit.db`

```python
db.execute("create table note (id serial primary key, body text)")
db.execute("insert into note (body) values (%s)", ["hello"])
rows = db.query("select * from note")     # -> list[dict]
with db.transaction() as tx:
    tx.execute("update note set body = %s where id = %s", ["hi", 1])
```

A `psycopg` connection pool that returns **dict rows**. Use `%s` placeholders in
both backends. In `azure` mode the password is an AAD access token from the
managed identity, fetched **per connection** (those tokens expire after about an
hour, so a pool that captured one would stop being able to open connections); in
`fake` mode it is a process-local in-memory SQLite database with the same
surface.

> **The fake is SQLite, not Postgres.** `serial`, `returning`, `now()`,
> `boolean`, `timestamptz` and a literal `%` in SQL all behave differently.
> `tests/test_db_conformance.py` runs one suite against both and marks each known
> divergence — read it before relying on anything beyond plain
> `select`/`insert`/`update`.

### `appkit.auth`

```python
u = auth.user(request)      # request: anything with a `.headers` mapping
if u and u.has_role("approver"):
    ...
```

Reads the Azure Container Apps Easy Auth headers
(`X-MS-CLIENT-PRINCIPAL[-NAME|-ID|-IDP]`) and returns a `User`
(`id`, `name`, `email`, `provider`, `roles`). Locally it returns a configurable
**dev user** so pages render without a login.

#### Those headers are only as trustworthy as what is in front of you

Easy Auth strips client-supplied `X-MS-CLIENT-PRINCIPAL*` headers and injects
its own, so **behind it** they are trustworthy. Any request path that skips it —
internal ingress, another container in the same environment, a directly exposed
port, or Easy Auth left in "allow unauthenticated" mode — lets the caller set
those headers by hand, and with them `has_role()`.

appkit cannot detect that from inside the container, so `APPKIT_AUTH` says how
the user is established:

| `APPKIT_AUTH` | Behaviour |
| --- | --- |
| `easyauth` | Trust the platform-injected headers. **Only safe if Easy Auth is enabled and set to reject unauthenticated requests.** |
| `verify` | Ignore those headers; cryptographically verify the `X-MS-TOKEN-AAD-ID-TOKEN` JWT against the tenant's signing keys. Forged headers cannot survive this. |
| `public` | Nobody is signed in. The headers are ignored rather than trusted, so `user()` always returns `None` and forging one gains nothing. For apps that are deliberately anonymous. |
| `dev` | The local dev user, plus header simulation. **Refused** on an Azure app platform. |

Unset, it follows the backend (`fake` → `dev`, `azure` → `easyauth`) — but on
Container Apps or App Service it must be set explicitly, so that trusting a
proxy is always a decision somebody made on purpose.

`verify` is the only one of the three that does not depend on trusting the
network in front of the app. It needs the `appkit[verify]` extra, Easy Auth's
**token store enabled** (so the id token is forwarded), and:

| Variable | Meaning |
| --- | --- |
| `APPKIT_AUTH_TENANT_ID` | Entra tenant the token must come from. |
| `APPKIT_AUTH_CLIENT_ID` | The Easy Auth app registration's client id (the token's `aud`). |
| `APPKIT_AUTH_AUTHORITY` | Login endpoint; defaults to the public cloud. |

## Configuration

Only `APPKIT_SHAREPOINT_FAKE_DIR`, `APPKIT_EMBEDDINGS_ENDPOINT`, and
`APPKIT_CHAT_ENDPOINT` apply to the fake backend; the rest are
needed in `azure` mode:

| Variable | Used by | Meaning |
| --- | --- | --- |
| `APPKIT_BACKEND` | all | `fake` (default) or `azure`. |
| `APPKIT_SHAREPOINT_SITE` | sharepoint | Graph site id. |
| `APPKIT_SHAREPOINT_FAKE_DIR` | sharepoint | Folder of `<ListName>.xlsx` / `<ListName>.csv` list exports (fake backend only). |
| `APPKIT_MAIL_SENDER` | mail | Mailbox to send as. |
| `APPKIT_EMBEDDINGS_ENDPOINT` | embeddings | Azure OpenAI resource endpoint, e.g. `https://x.openai.azure.com`. Setting it turns embeddings on, on either backend. |
| `APPKIT_EMBEDDINGS_DEPLOYMENT` | embeddings | Deployment name (default `text-embedding-3-small`). |
| `APPKIT_EMBEDDINGS_API_VERSION` | embeddings | REST API version (default `2023-05-15`). |
| `APPKIT_CHAT_ENDPOINT` | chat | Azure OpenAI resource endpoint. Setting it turns chat on, on either backend, independently of `APPKIT_EMBEDDINGS_ENDPOINT`. |
| `APPKIT_CHAT_DEPLOYMENT` | chat | Deployment name (default `gpt-4o-mini`). |
| `APPKIT_CHAT_API_VERSION` | chat | REST API version (default `2023-05-15`). |
| `APPKIT_DIRECTORY_TOP` | directory | Graph page size (default 200). |
| `APPKIT_DIRECTORY_EMPLOYEE_FILTER` | directory | Optional OData fragment applied when `employees_only=True`; without it the filtering happens in Python. |
| `APPKIT_DNS_SERVER` | dns | Resolver to query. Defaults to the system resolver. |
| `APPKIT_DNS_TIMEOUT` / `APPKIT_DNS_LIFETIME` | dns | Per-query and total lookup budget, in seconds (defaults 2 and 4). |
| `APPKIT_DB_DSN` | db | Postgres connection string (no password — the token is injected). |
| `APPKIT_DB_POOL_MAX` | db | Max pool size (default 10). |
| `APPKIT_AUTH` | auth | `easyauth`, `verify`, `public` or `dev`. Required on an Azure app platform. |
| `APPKIT_AUTH_TENANT_ID` / `APPKIT_AUTH_CLIENT_ID` / `APPKIT_AUTH_AUTHORITY` | auth | Only for `APPKIT_AUTH=verify`. |
| `APPKIT_DEV_USER` / `APPKIT_DEV_EMAIL` / `APPKIT_DEV_ROLES` / `APPKIT_DEV_SHORTNAME` | auth | The local dev user (`APPKIT_AUTH=dev` only). |
| `APPKIT_GRAPH_CLIENT_ID` / `APPKIT_GRAPH_CLIENT_SECRET` / `APPKIT_GRAPH_TENANT_ID` | sharepoint, mail, directory | Sign in to Graph as this app registration instead of the managed identity. All three or none; the secret comes from Key Vault, never from code. |
| `APPKIT_DIRECTORY_PROBE` / `APPKIT_DNS_PROBE` | doctor | A name fragment and a hostname the doctor should look up. Both checks skip unless set, so nothing reaches the network unasked. |

The managed identity needs, at minimum: Graph `Sites.Read.All` (SharePoint),
`Mail.Send` (mail), `User.Read.All` (directory), an AAD role on the Postgres
server (db), and **Cognitive Services OpenAI User** on the Azure OpenAI
resource (embeddings).

**Graph as an app registration.** At UZH the Entra team grants Graph
application permissions such as `Sites.Selected` to app registrations, not to
managed identities. Set the three `APPKIT_GRAPH_*` variables and Graph calls
(SharePoint, mail, directory) sign in as that registration, while Postgres and
Azure OpenAI stay on the managed identity. It is deliberately not
`AZURE_CLIENT_SECRET`: that would move the whole process, and the Postgres role
belongs to the managed identity. The startup report says which one is in use:

```text
config | graph        PASS  app registration 64398970-… (client secret)
```

## Errors

Anything appkit raises on purpose derives from `AppkitError`:

```python
from appkit import GraphError, sharepoint

try:
    rows = sharepoint.list_rows("Requests")
except GraphError as exc:
    exc.status, exc.code, exc.message, exc.request_id
```

`GraphError` keeps the Graph error body and the `request-id` header — the two
things you need to explain a 403 or a 404, and the value Microsoft support asks
for. Graph requests are retried with backoff on throttling (`429`, honouring
`Retry-After`) and transient server errors; `sendMail` is only retried when Graph
tells us it did *not* process the request, since sending is not idempotent.
Configuration problems raise `ConfigError`.

`AzureOpenAIError` is the same idea for `appkit.embeddings`, and its message
names the likely cause: a missing role assignment on 401/403, the deployment
name on 404, and — for the `SubscriptionNotRegistered` 400 — the
`az provider register --namespace Microsoft.CognitiveServices` the caller's
subscription is missing.

## Checking a deployment

```sh
python -m appkit.doctor                       # or: appkit-doctor
python -m appkit.doctor --json                # for a Container Apps Job
python -m appkit.doctor --list requests --send-mail you@uzh.ch
```

Run it in the container, as the app. It reports the active backend and auth
mode, which credential answered and whether its token is application or
delegated, the Graph app roles it actually carries, the SharePoint lists it can
see (with the `name` and `id` Graph will accept, which is *not* the display
name), and whether the database answers. Checks run independently so one broken
thing doesn't hide the rest, nothing is contacted on the `fake` backend, and no
token, password or DSN is ever printed. Exit code is 1 if anything failed, so it
works unattended as a Container Apps Job.

The two ids it prints are the ones you need to grant it anything: the **client
id** for a `Sites.Selected` site grant, the **object id** for the Graph app-role
assignment.

## Development

```sh
uv sync --extra dev      # or: uv pip install -e '.[dev]'
uv run pytest            # fake backend + Graph contract tests, no network
uv run ruff check .
```

Tests reset all fake state between cases via the `reset_fakes()` helper (wired
up as an autouse fixture in `tests/conftest.py`).

### Testing the real backends

The suite is layered, because the fake proving something says nothing about
Azure:

| Layer | What it proves | Needs |
| --- | --- | --- |
| `test_*.py` (fake) | The in-memory behaviour apps develop against. | nothing |
| `test_graph_contract.py`, `test_*_azure.py` | The real Graph code path: URLs, `$expand`/`$top`, pagination, retry policy, error mapping. Runs against a mocked transport with only the managed-identity token stubbed. | nothing |
| `test_db_conformance.py` | One database suite run against **both** the fake and a real Postgres, through the real `psycopg` pool. Divergences are marked `xfail`, not hidden. | a Postgres |
| `test_auth_verify.py` | `APPKIT_AUTH=verify` end to end: a real RSA key signs real JWTs, a real local HTTP server serves the JWKS, and forged, tampered, expired, wrong-audience and wrong-tenant tokens are all rejected. | nothing |
| `tests/live/` | Real Graph and real Azure Postgres — see below. | Azure |

#### The live suite

```sh
pytest -m live          # opt-in; excluded from a plain `pytest` run
pytest -m soak -s       # over an hour; proves the token-refresh fix
```

It refuses to start unless `APPKIT_BACKEND=azure`, because a live run that
quietly used the fakes is worse than no run at all. Each area skips loudly when
its configuration is missing rather than substituting anything:

| Variable | Enables |
| --- | --- |
| `APPKIT_SHAREPOINT_SITE` + `APPKIT_LIVE_LIST` | SharePoint reads, projection, pagination, error shapes |
| `APPKIT_MAIL_SENDER` + `APPKIT_LIVE_MAIL_TO` | actually sending mail (opt-in — it reaches a real mailbox) |
| `APPKIT_DB_DSN` | Postgres over an Entra token, TLS, per-connection token fetch |
| `APPKIT_LIVE_FORBIDDEN_SITE` / `APPKIT_LIVE_FORBIDDEN_SENDER` | that `Sites.Selected` and an Exchange `ApplicationAccessPolicy` really do fence the identity in |

`tests/live/test_soak.py` is the one no other layer can replace. Entra tokens
expire after about an hour, and the pool bug this library had only appears
*after* that — every unit test, CI job and short live run finishes inside the
token's lifetime. The soak test deliberately outlives the token, then forces the
pool to open a new connection and asserts it succeeds with a different token.

Run both as Container Apps Jobs with `live.Dockerfile`, which starts by running
`appkit-doctor` — if the environment is wrong, its report explains why in a way
a test failure would not.

**Still uncovered:** Easy Auth itself. It needs a browser, a login and a request
through the platform proxy, so no job can reach it. One browser login against a
deployed app settles the two open questions — whether Container Apps forwards
`X-MS-TOKEN-AAD-ID-TOKEN` at all, and whether the claim names match what
`_jwt.py` reads.

```sh
docker run -d --name appkit-pg -p 5432:5432 \
    -e POSTGRES_USER=appkit -e POSTGRES_PASSWORD=appkit -e POSTGRES_DB=appkit \
    postgres:16
APPKIT_TEST_PG_DSN=postgresql://appkit@localhost:5432/appkit uv run pytest
```

Without `APPKIT_TEST_PG_DSN` the Postgres half skips, so the default `uv run
pytest` still needs nothing but Python. CI runs both.

Not yet covered: a live Microsoft 365 tenant (real Graph, real SharePoint list,
real mailbox) and the Container Apps Easy Auth headers. The Easy Auth parser
should be pinned with a golden `X-MS-CLIENT-PRINCIPAL` blob captured once from a
real deployment — see `tests/test_auth.py`, which currently builds its own.
