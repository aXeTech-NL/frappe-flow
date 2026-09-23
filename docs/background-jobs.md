# Background AI jobs

`Flow Job` is a durable database worklist for System One decisions, agent runs, and document/scheduled triggers. Redis/RQ executes only a bounded number at a time. Interactive chat remains an HTTP/SSE web request; it does not wait behind bulk jobs.

## Setup

After updating the app and its requirements, **merge** this custom worker registration into `sites/common_site_config.json`, preserving any existing `workers` entries and other configuration:

```json
{
  "workers": {
    "flow": {"timeout": 900, "background_workers": 4}
  }
}
```

Frappe validates queue names against this configuration. Without registration, both publishing to `flow` and starting `bench worker --queue flow` fail; durable jobs remain available for recovery. Flow does not change bench configuration automatically. Then:

```bash
bench --site <site> migrate
bench --site <site> enable-scheduler
bench worker --queue flow
```

The last command is a foreground development worker. Regenerate/reload worker process configuration and start persistent workers for `flow` using your production process manager. Keep the usual Frappe scheduler and default-queue workers running too: they drive periodic dispatch and maintenance. The queue name is configurable in **Flow Settings**; register a matching `workers` entry and have workers listen to that configured queue. Changing it does not move already published jobs.

Migration initializes settings/indexes; it sends no AI requests. Existing document and scheduled Flow Triggers now create Flow Jobs, so configure the dedicated worker before expecting them to execute. Previously published legacy trigger jobs may still finish through the old queue; drain those before upgrading to avoid overlapping old and new work.

## Lifecycle

```text
application transaction → Pending database rows
                             ↓
                     bounded reservation
                             ↓
                       Queued in RQ
                             ↓
                          Running
                   ↙         ↓         ↘
               Paused    Completed    Failed
```

A disabled trigger, missing target document, or unmet condition can produce `Cancelled`. Retryable decision failures go back to `Pending` with a future `next_attempt_at`.

- Registration does not publish to Redis/RQ, emit per-job document notifications, enqueue a callback, or commit the caller's transaction. Normal Frappe metadata/permission caching still applies. Rolling back an insert also rolls back its jobs.
- A dispatcher runs every minute. Finishing workers also refill the queue, avoiding a one-minute delay between every small batch.
- A database row lock serializes dispatchers. At most `Maximum In-flight Jobs` are reserved as Queued + Running per site. Extra workers cannot exceed this cap.
- Reservations commit before RQ publication. If publication is interrupted, the database rows remain available for reconciliation.
- Workers atomically claim a reservation token and commit Running before any AI call. Duplicate or obsolete deliveries cannot claim the same job again. No row lock is held across model calls.
- Missing unstarted RQ jobs are safely rescheduled after a grace period, with old delivery tokens invalidated. A Redis outage is not treated as evidence that work disappeared.
- Unknown/interrupted Running executions become Failed for manual review only after the RQ job has ended/disappeared and the appropriate grace period. A job still reported Started is never reclaimed into concurrent execution; investigate an orphaned Started RQ job using Frappe/RQ administration.
- Agent jobs link to `Flow Run` before the model executes. Recovery preserves a confirmed linked Paused/Completed/Failed result if a worker dies before recording its job outcome. Paused runs release queue capacity and stay Paused until a later reconciliation sees their linked run completed/failed.
- Approval continuations run directly, including SSE streaming, rather than rejoining the bulk queue. They retain the original execution user and trigger recursion guard, even when an authorized manager approves them. Their interactive execution is outside the bulk in-flight cap.

The queue does **not** guarantee exactly-once external actions. A worker can die after sending an email or changing a remote system. Inspect a failed agent's linked run before deliberately submitting new work. Independent jobs for the same document can run concurrently; applications must enforce business-level ordering and safe updates when needed.

## Settings

| Flow Settings field | Default | Meaning |
| --- | --- | --- |
| Dispatch Background Jobs | On | Pause new reservations without discarding backlog; already queued/running work can finish |
| Worker Queue | `flow` | Dedicated RQ queue; requires a listening worker |
| Maximum In-flight Jobs | 4 | Queued + Running per site, 1–64 |
| Worker Timeout | 900 seconds | RQ execution timeout, not provider connect/read timeout |
| Maximum Decision Attempts | 3 | Bounded retries for transient read-only decisions; 1 disables retries |
| Keep Completed / Cancelled Jobs | 30 days | Based on finished_at; zero keeps them indefinitely |
| Keep Failed Jobs | 90 days | Based on terminal failure time; zero keeps them indefinitely |

This is a concurrency limit, **not a provider RPM/TPM limiter or a reserved chat quota**. Agent jobs can make multiple provider calls. Bulk work and direct chat still share server resources and provider quotas. Keep concurrency conservative, use separate provider accounts/deployments where needed, and monitor 429 responses. Multiple sites each have their own cap.

Decision failures classified as rate_limit, unavailable, timeout, or transport are retried after 30, 60, 120… seconds (capped at an hour), up to the configured attempt limit. Waiting does not occupy a worker. An uncertain timeout can still incur duplicate provider billing. Authentication, malformed responses, permission errors and ordinary agent/trigger failures are not automatically retried. Per-task limits are captured when reserving each attempt.

## Frappe app API

### Background decision

```python
from flow.decisions import submit
from flow.jobs import get_job

job_name = submit(
    "ticket_triage",
    state={"subject": "I was charged twice"},
    idempotency_key="ticket-0001:analysis:v1",
)
# The surrounding request/job commits normally. Do not wait for completion inside it.
# In a later request or server task:
job = get_job(job_name)
if job["status"] == "Completed":
    answers = job["result"]["answers"]
```

`submit` also accepts the same inline provider/questions/model arguments as `evaluate`. It authorizes them before storing the job, and execution checks permissions again as the original user. `evaluate` remains synchronous and does not persist its input/result.

### Background agent

```python
from flow.jobs import submit_agent

job_name = submit_agent(
    "Ticket analyst",
    "Analyze ticket TICKET-0001 and report your findings.",
    auto_approve=False,
    idempotency_key="ticket-0001:agent:v1",
)
```

Only named Flow Agents are supported; jobs cannot import arbitrary callables. `auto_approve=False` preserves confirmation pauses. Opting into automatic tool approval requires the same care as unattended triggers. Agent history lives in the linked Flow Run/Session, not in a duplicate job transcript.

### Idempotency and triggers

An optional explicit key is scoped to job type and execution user. The same key and identical request return the existing job; reusing it with different input raises an error. Without a key, each submission intentionally creates new work. Deduplication lasts as long as that Flow Job is retained: it is not an eternal business ledger. Database transaction conflicts propagate normally; retry the entire surrounding transaction, never only the job insertion after a database rollback.

Document trigger identity includes trigger name, target document, lifecycle event and document revision. Different triggers/events/revisions remain separate. Scheduled occurrence registration and `last_fired_at` update share a transaction. Schedule behavior remains coalescing: overdue occurrences produce one current job, not a catch-up storm for every missed interval.

Trigger workers retain the existing execution-time behavior: they reload the **current** document, recheck the current condition and render the current prompt. They do not store a snapshot of the original document. If a document changes while waiting, analysis sees its later contents. Applications needing immutable-event analysis should submit an explicit decision state instead.

Internal Flow records do not generate Flow trigger work. The running background trigger also suppresses same-trigger/same-document recursion during its own tool execution. Cross-trigger cycles and other application hooks are still the application's responsibility.

## Permissions, privacy, and maintenance

Configuration and Desk job lists default to System Manager access. Job payload, identity, status and delivery tokens cannot be changed through normal document editing. Submission and result APIs are server-only, not generic whitelisted HTTP endpoints; consuming apps own their exposed endpoints and input validation.

`get_job` allows the execution user or a user with Flow Job read permission. A low-privilege user whose document fires a privileged trigger does not automatically gain access to that trigger's results. Guest/disabled users cannot execute jobs. Triggers freeze their configured execution identity; if that identity changes before execution, the old job fails rather than silently adopting new privileges.

Background decision state, agent prompts and decision results are persisted in Flow Job. Do not include credentials or unnecessary sensitive fields. Saved definitions/provider configuration are resolved at execution, not snapshotted. Keep API keys only in their existing encrypted provider fields. Flow Job cleanup does not affect external provider retention.

Daily cleanup removes at most 1,000 old completed/cancelled and 1,000 old failed jobs per pass, in batches of 100. It never deletes Pending, Queued, Running, Paused or retryable jobs, even if they are very old. Large backlogs/cleanup catch-up require monitoring; retention is not a hard database-size cap. Deleting old jobs does not delete linked Flow Runs or Sessions. Session cleanup also preserves history linked to unfinished background jobs, including old confirmation pauses; other session history retains its independent retention policy.

To diagnose a stuck backlog, check scheduler health, default-queue workers, the dedicated worker, Redis connectivity, Flow Settings dispatch switch/cap, and Queued/Running RQ jobs. Do not simply reset Running rows to Pending: that can duplicate side effects. After investigating a terminal failure, submit intentionally new work with a new idempotency key.

## Tests

```bash
bench --site <test-site> run-tests --module flow.tests.test_jobs
bench --site <test-site> run-tests --doctype "Flow Job"
bench --site <test-site> run-tests --module flow.flow.doctype.flow_job.test_job_resume
bench --site <test-site> run-tests --module flow.tests.test_ai_triggers
bench --site <test-site> run-tests --doctype "Flow Trigger"
```

The database suite includes 4,000 registrations without enqueueing, rollback, idempotency, two-worker claims, concurrent dispatch caps, and terminal-only cleanup. Use an isolated test site; tests commit deliberately when exercising separate database connections. No live AI credentials are required.
