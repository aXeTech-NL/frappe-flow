<div align="center">

# Flow

**AI chat, reusable decisions, and background automation for Frappe.**

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![CI](https://github.com/aXeTech-NL/frappe-flow/actions/workflows/ci.yml/badge.svg)](https://github.com/aXeTech-NL/frappe-flow/actions/workflows/ci.yml)

</div>

<img width="1470" height="923" alt="Flow assistant in Frappe Desk" src="https://github.com/user-attachments/assets/59749f10-ccc7-49ce-aa30-9d0dc46c117a" />

Flow puts an AI assistant inside Frappe and exposes reusable Python APIs for other apps. This repository builds on [Frappe Flow](https://github.com/frappe/flow_client).

## Three ways to use Flow

| Mode | Purpose | Execution |
| --- | --- | --- |
| **Interactive chat** | Ask questions, work with documents, use tools and knowledge bases | Direct web request with HTTP/SSE streaming; not the background queue |
| **System One decisions** | Classify, score, and assess yes/no probabilities using your own questions | Direct `evaluate()` or background `submit()` |
| **Background automation** | Document/scheduled triggers and bulk decision or agent work | Durable `Flow Job` records, processed gradually through Frappe/RQ |

The chat panel opens with **Ctrl+I** / **Cmd+I** from Frappe Desk. Agents can read and update records through permission-aware tools, use retrieved knowledge, and request human confirmation before actions. Your application still owns business rules and approval requirements.

> “List overdue sales orders and summarize what needs attention.”
>
> “Every weekday morning, summarize yesterday's new leads.”

## Installation and update

Use an existing Frappe bench. The current project requires Python 3.14; see `pyproject.toml` for Frappe and dependency versions. Select the branch you intend to deploy—features on a development branch are not automatically present on a release branch.

```bash
cd /path/to/frappe-bench
bench get-app --branch <your-flow-branch> https://github.com/aXeTech-NL/frappe-flow.git
bench --site <site> install-app flow
```

When updating an existing installation, update the checkout/requirements and run:

```bash
bench --site <site> migrate
```

### Chat setup

Create a **Flow Provider** with credentials, then a **Flow Model** with a model ID such as `anthropic/claude-sonnet-4-6`. For a local chat provider, configure its Base URL. Open the Desk panel to start chatting. Chat uses the existing LiteLLM model path, separate from System One providers.

### Background worker setup

Document/scheduled triggers and asynchronous submissions require both the ordinary Frappe scheduler/default workers and a worker listening to the configured Flow queue. First **merge** a custom worker entry into `sites/common_site_config.json` (preserve existing configuration and other worker entries):

```json
{
  "workers": {
    "flow": {"timeout": 900, "background_workers": 4}
  }
}
```

Frappe must know this queue before it will accept jobs or start its worker. Then, for development:

```bash
bench --site <site> enable-scheduler
bench worker --queue flow
```

The worker command runs in the foreground for development. Regenerate/reload the bench's worker process configuration as appropriate for your production process manager, and configure persistent workers. In **Flow Settings**, configure the queue, maximum in-flight jobs, worker timeout, decision retry limit and retention periods. Migration alone does not start a worker.

## System One decisions

System One models return typed answers rather than chat text:

- **Choice:** select one of your options and return its probability distribution.
- **Score:** rate against ordered levels, returning a possibly fractional level index.
- **Noul:** return the probability that the answer is yes.

Create a **Flow Decision Provider** with your own complete HTTP endpoint, optional bearer API key and model ID. Jev, self-hosted Laya, and other compatible services use the same interface; Flow does not hardcode vendors or install model weights.

Save a **Flow Decision**, for example `ticket_triage`, with its questions and criteria. Another Frappe app can call it directly:

```python
from flow.decisions import evaluate

result = evaluate("ticket_triage", state={"subject": "I was charged twice"})
team = result["answers"]["team"]["choice"]
```

Or register work for later processing without waiting for the provider:

```python
from flow.decisions import submit
from flow.jobs import get_job

job_name = submit(
    "ticket_triage",
    state={"subject": "I was charged twice"},
    idempotency_key="ticket-0001:triage:v1",
)
# In a later request, after the submission transaction has committed:
job = get_job(job_name)
```

The examples assume you have configured the named definition and its `team` question. Apps authorize the documents they read and select the state to send. Flow checks access to definitions/providers and validates structured results. Thresholds and side effects stay in the consuming app.

See **[System One setup and app integration](docs/system-one.md)** for schemas, permissions and provider configuration.

## Bulk jobs and triggers

An import of 4,000 documents can create 4,000 pending `Flow Job` records in the database without publishing 4,000 RQ jobs at insertion time. The dispatcher reserves only the configured number of in-flight jobs (default 4), and workers refill the queue as work finishes.

The job list shows Pending, Queued, Running, Paused, Completed, Failed and Cancelled states. Jobs survive Redis publication failures. Reservation tokens prevent duplicate delivery from concurrently executing the same job. Agent jobs link to their existing **Flow Run**, including confirmation pauses.

Daily cleanup uses the **completion date**, not creation: completed/cancelled jobs default to 30 days and failed jobs to 90 days. Pending, active and paused work is never discarded merely because it is old. Retention does not remove linked agent history.

Direct chat does not join this bulk queue. However, chat and bulk work can still share provider quotas and server resources. A job concurrency limit is not a requests/tokens-per-minute limiter, nor a guarantee of exactly-once external actions. Failed/uncertain agents require review before resubmission; only transient decision failures receive bounded automatic retries.

See **[Background jobs: operation, settings and app APIs](docs/background-jobs.md)** for deployment, recovery and limits.

## Agent framework

### Define a tool

```python
import frappe
from flow import tool

@tool
def get_open_todos(allocated_to: str) -> list[dict]:
    """List open ToDo items for a user, subject to current-user permissions."""
    return frappe.get_list(
        "ToDo",
        filters={"allocated_to": allocated_to, "status": "Open"},
        fields=["name", "description", "status"],
    )
```

### Run an agent directly

```python
from flow import Agent

agent = Agent(
    model="anthropic/claude-sonnet-4-6",
    instructions="You help with Frappe tasks.",
    tools=[get_open_todos],
)
result = agent.run("List my open ToDo items")
print(result.output)
```

Use `agent.new_session()` and `session.chat(...)` to persist a conversation. For background execution of a saved Flow Agent:

```python
from flow.jobs import submit_agent

job_name = submit_agent("Ticket analyst", "Analyze ticket TICKET-0001")
```

### DocTypes

| DocType | Purpose |
| --- | --- |
| Flow Provider / Flow Model | Chat-provider credentials and model configuration |
| Flow Agent / Flow Tool | Agent instructions and reusable tools |
| Flow Trigger | Document-event or scheduled agent automation |
| Flow Decision Provider / Flow Decision | System One connections and reusable typed questions |
| Flow Job / Flow Settings | Durable background work, concurrency, retry and retention settings |
| Flow Knowledge Base / Flow Knowledge Source | Retrieval sources: files, URLs and documents |
| Flow Session / Flow Run | Conversations and agent execution history |

## License

[GNU AGPLv3](license.txt)
