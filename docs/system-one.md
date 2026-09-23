# System One decisions for Frappe apps

Flow's System One layer evaluates content against **Choice**, **Score**, and **Noul** questions. It returns structured answers, not chat text. It is separate from `Flow Model`, LiteLLM, agents, sessions, and tools: no LLM or agent needs to run first.

Any provider implementing the System One HTTP contract can be configured. No vendor names, API hosts, or model IDs are hardcoded. Multiple connections to the same server are allowed. Flow does not install model weights or an inference runtime.

## Configure in Desk

After installing/updating Flow, run `bench --site <site> migrate` to sync the new DocTypes. In the Flow workspace, open **Flow Decision Provider** (also under **System One** in the sidebar).

Create a connection with:

- **Provider Name:** your own name, e.g. `Decision service production`.
- **Endpoint URL:** the **complete evaluation URL**, including `/v1/systemone` or your server's equivalent path. Flow does not append a path.
- **API Key:** optional printable ASCII bearer token without whitespace, stored in a Frappe Password field. It is not returned in decision results.
- **Model ID:** the server's model name; optional only if that server selects a default.
- **Timeout:** connect/read timeout, 1–300 seconds (default 30). This is not a total wall-clock deadline.
- **Enabled:** disabling the connection blocks all decisions using it.

Save and use **Test Connection**. This sends a small fixed Noul question and may incur provider usage; it sends no application records. It tests saved settings only.

Examples, not restrictions:

| Service | Endpoint URL | Model ID | Authentication |
| --- | --- | --- | --- |
| TypeSafe Jev | `https://api.typesafe.ai/v1/systemone` | e.g. `jev-latest` or a pinned version | Bearer API key |
| Self-hosted Laya | `http://your-laya-server:8000/v1/systemone` | e.g. `english`, `multilingual`, `typed-decisions`, or blank for server routing | Bearer token if configured on the server |
| Your own compatible service | Your complete evaluation URL | Whatever your server supports | Optional bearer token |

Use HTTPS for remote services. Only administrators/trusted configuration authors should set endpoints: local/private addresses are deliberately allowed for self-hosting. Redirects are rejected, TLS verification stays enabled, and environment proxies and `.netrc` credentials are not used. Laya's server authentication is optional by default; configure it and restrict network access before using it outside a trusted development network.

### Reusable decision definition

Create a **Flow Decision** with title, stable snake_case **Slug** (for example `ticket_triage`), provider, and these **Questions**:

```json
{
  "team": {
    "type": "choice",
    "instructions": "Which team should handle this ticket?",
    "criteria": {
      "billing": "Payments, invoices and refunds",
      "technical": "Bugs and technical problems",
      "other": "None of the above"
    }
  },
  "urgency": {
    "type": "score",
    "instructions": "How urgent is this ticket?",
    "criteria": ["Normal", "Urgent", "Critical"]
  },
  "refund": {
    "type": "noul",
    "instructions": "Does the customer request a refund?"
  }
}
```

An optional Model ID overrides the provider default for this definition. All questions evaluate the same state. Questions are data: Flow does not execute scripts, resolve DocTypes, or render template expressions in them. No decision definitions or providers are installed automatically.

## Call from another Frappe app

Flow must be installed on the same site. An app that depends on this API can declare `required_apps = ["flow"]` in its `hooks.py`.

```python
import frappe
from flow.decisions import evaluate


def classify_ticket(ticket_name: str):
    ticket = frappe.get_doc("Support Ticket", ticket_name)
    ticket.check_permission("read")

    result = evaluate(
        "ticket_triage",
        state={"subject": ticket.subject, "description": ticket.description},
    )
    return result["answers"]
```

The ticket DocType/fields are illustrative: use your app's own schema. Your app selects and authorizes the data sent to the provider. The same definition can be used by different apps without changing Flow.

The document form also works: `frappe.get_doc("Flow Decision", "ticket_triage").evaluate(state)`. It always reloads the saved definition rather than using unsaved object modifications.

### Questions defined in code

For callers allowed to use a provider directly:

```python
from flow.decisions import choice, evaluate, noul, score

result = evaluate(
    provider="Decision service production",
    state={"message": "I was charged twice. Please refund the duplicate payment."},
    questions={
        "team": choice("Which team?", {"billing": "Payments", "technical": "Bugs", "other": None}),
        "urgency": score("How urgent?", ["Normal", "Urgent", "Critical"]),
        "refund": noul("Is a refund requested?"),
    },
)
```

Plain dictionaries work too; helpers are optional. Each evaluation requires at least one named question, even if a provider supports empty/no-op requests. Instructions and rubric descriptions can also be JSON objects/arrays. Choice descriptions may be `null`; Noul can optionally describe `true` and `false`. Provider-specific context/question/option limits are enforced by the server, not a vendor-specific list in Flow.

Do not combine a saved decision name with inline questions, provider, or model overrides. A saved definition delegates only its configured questions and connection.

## Results and failure handling

Results retain the provider's JSON envelope: `model`, `answers`, optional `usage`, and any additional provider metadata. Every question must have exactly one answer with the matching type:

- **Choice:** `choice` is a supplied option key; `probabilities` covers every option; `confidence` is in `[0, 1]`.
- **Score:** `score` is the probability-weighted **zero-based level index**. For three levels its range is `[0, 2]`, not `[0, 1]`. `legend`, `probabilities`, and `confidence` are preserved.
- **Noul:** `noul` is P(yes) in `[0, 1]`. There is no fabricated boolean or separate confidence field.
- **Usage:** `input_tokens` and `output_tokens` may be missing/null. Missing usage is not replaced with a fabricated zero.

Flow validates answer IDs, types, option sets, probability ranges/sums, selected choices, and score/legend consistency. Invalid or partial responses fail the entire call. It never substitutes a default decision or automatically switches providers.

```python
from flow.decisions import DecisionError, evaluate

try:
    result = evaluate("ticket_triage", state={"subject": "Please help"})
except DecisionError as exc:
    # Your app chooses whether to defer, retry, or require human review.
    # exc.kind: configuration, authentication, validation, rate_limit,
    #           unavailable, timeout, transport, http, invalid_response
    # exc.status_code is present for HTTP errors (otherwise None).
    raise
```

Invalid caller input raises `ValueError`; normal Frappe missing-document and permission exceptions are not hidden. Network/provider errors have safe messages without echoed request data, provider error bodies, URLs, or credentials. Responses are limited to 4 MiB. There are **no automatic retries**: callers own retry limits and backoff, including 429/529 handling, and should account for duplicate billing after an uncertain timeout.

Probabilities/confidence are model outputs, not authorization or guarantees of correctness. Your app owns thresholds, language/domain evaluation, human review and all side effects. An AI result must never bypass document permissions, workflow validation or explicit approval requirements. Evaluate provider quality on representative data before automating consequential actions.

## Permissions and privacy

Both configuration DocTypes default to System Manager access. Configuration authors need read access to the selected provider. Grant other roles/users **read access to specific Flow Decisions** through Frappe Role Permissions or document sharing to allow use of those definitions without giving them provider-level access. Read access delegates provider usage and can incur cost; grant it deliberately.

Inline calls require read access to **Flow Decision Provider**, so they are intended for trusted roles/server jobs. Guest users are rejected. `evaluate` and the decision document's `evaluate` method are **not whitelisted HTTP endpoints**. A consuming app can expose its own purpose-specific endpoint with its own permission and input controls. There is no unrestricted generic public REST API.

Flow does not automatically load documents, save decision inputs/results, or attach them to chat history. The configured service receives the supplied state/questions and follows its own retention policy. Avoid sending whole documents or secrets unnecessarily. Apps can add their own audit records or background jobs; those policies are not imposed by this generic layer.

`SystemOneClient` is the low-level transport for trusted Python code with explicit connection parameters. It bypasses DocType lookup/authorization by design; use `evaluate` for normal Frappe app integration. Do not expose the low-level client with user-supplied URLs or credentials.

## Contract references and validation

- [TypeSafe HTTP API](https://docs.typesafe.ai/api)
- [Laya self-hosted HTTP server](https://github.com/NandhaKishorM/laya#self-hosting-http-server-jev-compatible)

The common HTTP contract is implemented without either vendor's SDK. Compatible wire formats do not imply equal model behavior or provider limits. Jev requires a model; Laya can route automatically and may route unknown model names instead of rejecting them. Configure and test model IDs explicitly when reproducibility matters.

Tests use mocked transport and Frappe integration fixtures; live provider access is not required:

```bash
bench --site <test-site> run-tests --module flow.tests.test_decisions
bench --site <test-site> run-tests --doctype "Flow Decision Provider"
bench --site <test-site> run-tests --doctype "Flow Decision"
```
