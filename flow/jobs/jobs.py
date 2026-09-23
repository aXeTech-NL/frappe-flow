"""Transactional outbox for bounded RQ execution; no network work at registration."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import timedelta
from functools import partial, wraps
from typing import Any
from uuid import uuid4

import frappe
from frappe.utils import get_datetime, now_datetime

ACTIVE = ("Queued", "Running")
TERMINAL = ("Completed", "Failed", "Cancelled")
TRANSIENT = frozenset({"rate_limit", "unavailable", "timeout", "transport"})
RECONCILE_LIMIT = 100
PUBLIC_FIELDS = (
	"name",
	"job_type",
	"status",
	"creation",
	"started_at",
	"finished_at",
	"next_attempt_at",
	"attempts",
	"run_as",
	"flow_run",
	"result",
	"error",
	"reference_doctype",
	"reference_name",
)


def _identity(user=None):
	user = user or frappe.session.user
	if user == "Guest" or not frappe.db.get_value("User", user, "enabled"):
		raise frappe.PermissionError("Flow jobs require an enabled, signed-in execution user.")
	return user


def _json(value):
	return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _register(kind, payload, *, run_as, idempotency_key=None, reference_doctype=None, reference_name=None):
	"""No commit, queue publication or document notifications; retain the caller's transaction."""
	encoded = _json(payload)
	fingerprint = hashlib.sha256(
		_json([kind, run_as, payload, reference_doctype, reference_name]).encode()
	).hexdigest()
	if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key.strip()):
		raise ValueError("Idempotency key must be non-empty text.")
	name = (
		hashlib.sha256(_json([kind, run_as, idempotency_key]).encode()).hexdigest()
		if idempotency_key
		else uuid4().hex
	)
	doc = frappe.get_doc(
		{
			"doctype": "Flow Job",
			"name": name,
			"creation": now_datetime(),
			"modified": now_datetime(),
			"modified_by": frappe.session.user,
			"job_type": kind,
			"status": "Pending",
			"run_as": run_as,
			"owner": run_as,
			"payload": encoded,
			"fingerprint": fingerprint,
			"attempts": 0,
			"next_attempt_at": now_datetime(),
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
		}
	)
	# Inputs/identity are validated by the closed submit APIs. Normal Document.insert would
	# emit thousands of cache/realtime notifications; this internal outbox needs only its row.
	# Frappe handles primary-key conflicts without aborting PostgreSQL transactions too.
	doc.db_insert(ignore_if_duplicate=True)
	if idempotency_key:
		# A current read sees the winning insert, unlike a stale repeatable-read snapshot.
		existing = frappe.db.get_value("Flow Job", name, "fingerprint", for_update=True)
		if existing != fingerprint:
			raise ValueError("Idempotency key was already used for different Flow job input.")
	# Database deadlocks must propagate: retrying only this function could lose the caller's
	# business writes. Never mask that error by releasing a savepoint invalidated by rollback.
	return name


def submit_decision(
	decision=None, *, state, provider=None, questions=None, model_id=None, idempotency_key=None
):
	from flow.decisions.schema import validate_questions, validate_state
	from flow.decisions.service import resolve_request

	user = _identity()
	_, resolved, _ = resolve_request(decision, provider=provider, questions=questions, model_id=model_id)
	validate_state(state)
	validate_questions(resolved)
	payload = {
		"decision": decision,
		"state": state,
		"provider": provider,
		"questions": questions,
		"model_id": model_id,
	}
	return _register("Decision", payload, run_as=user, idempotency_key=idempotency_key)


def submit_agent(agent: str, input: str, *, auto_approve: bool = False, idempotency_key=None):
	user = _identity()
	doc = frappe.get_doc("Flow Agent", agent)
	doc.check_permission("read")
	if not doc.enabled:
		raise ValueError("Flow Agent is disabled.")
	if not isinstance(input, str) or not input.strip():
		raise ValueError("Agent input must be non-empty text.")
	if type(auto_approve) is not bool:
		raise ValueError("Auto approve must be a boolean.")
	return _register(
		"Agent",
		{"agent": agent, "input": input, "auto_approve": auto_approve},
		run_as=user,
		idempotency_key=idempotency_key,
	)


def submit_trigger(*, trigger: str, target_doctype=None, target_name=None, event=None, version=None):
	"""Internal hook entry point; trusted trigger configuration determines the execution identity."""
	t = frappe.get_doc("Flow Trigger", trigger)
	if not t.enabled:
		return None
	user = _identity(t.run_as or t.owner)
	context = frappe.flags.get("flow_job_trigger")
	if context == (trigger, target_doctype, target_name):
		return None
	payload = {"trigger": trigger, "target_doctype": target_doctype, "target_name": target_name}
	key = _json([trigger, target_doctype, target_name, event, str(version)])
	return _register(
		"Trigger",
		payload,
		run_as=user,
		idempotency_key=key,
		reference_doctype=target_doctype,
		reference_name=target_name,
	)


def get_job(name: str) -> dict[str, Any]:
	"""Server-only result lookup. Trigger submitters do not inherit the run_as user's access."""
	user = _identity()
	doc = frappe.get_doc("Flow Job", name)
	if doc.run_as != user:
		doc.check_permission("read")
	result = {field: doc.get(field) for field in PUBLIC_FIELDS}
	result["result"] = json.loads(doc.result) if doc.result else None
	return result


def setup():
	"""Persist singleton defaults so its existing row can serialize dispatchers across commits."""
	if frappe.db.get_single_value("Flow Settings", "max_in_flight") is None:
		frappe.get_doc("Flow Settings").save(ignore_permissions=True)
	frappe.db.add_index("Flow Job", ["status", "next_attempt_at", "creation"], "flow_job_dispatch")
	frappe.db.add_index("Flow Job", ["status", "finished_at"], "flow_job_retention")
	frappe.db.add_index("Flow Job", ["flow_run"], "flow_job_run")


def _settings_lock():
	# Unlike an expiring Redis lock, this lock cannot expire while a dispatcher is reserving rows.
	# It is released by the reservation commit, BEFORE network publication and model execution.
	table = frappe.qb.DocType("Singles")
	rows = (
		frappe.qb.from_(table)
		.select(table.value)
		.where((table.doctype == "Flow Settings") & (table.field == "max_in_flight"))
		.for_update()
		.run()
	)
	if not rows:
		raise RuntimeError("Flow Settings are not initialized. Run bench migrate.")
	return frappe.get_doc("Flow Settings")


def _rq_status(job_id):
	from frappe.utils.background_jobs import get_job as get_rq_job

	job = get_rq_job(job_id)
	return job.get_status(refresh=True) if job else None


def _update(name, values):
	frappe.db.set_value("Flow Job", name, values, update_modified=True)


def _finish(name, status, *, error=None, **values):
	_update(
		name,
		{
			"status": status,
			"finished_at": now_datetime(),
			"error": error,
			"reservation": None,
			"rq_job_id": None,
			**values,
		},
	)


def _reconcile(now):
	"""Inspect old deliveries only; never write live active rows just to rotate a scan."""
	rows = frappe.get_all(
		"Flow Job",
		filters={"status": ["in", ACTIVE], "reserved_at": ["<=", now - timedelta(seconds=60)]},
		fields=["name", "status", "reserved_at", "timeout", "rq_job_id"],
		order_by="reserved_at asc",
		limit_page_length=RECONCILE_LIMIT,
	)
	for item in rows:
		status = _rq_status(item.rq_job_id)
		if status in ("queued", "started", "deferred", "scheduled"):
			continue
		row = frappe.get_doc("Flow Job", item.name, for_update=True)
		if row.status not in ACTIVE or row.rq_job_id != item.rq_job_id:
			continue
		if row.status == "Queued":
			_update(
				row.name,
				{
					"status": "Pending",
					"reservation": None,
					"rq_job_id": None,
					"next_attempt_at": now,
					"error": "Queue delivery unavailable; rescheduling unstarted work.",
				},
			)
		else:
			# The agent transaction may have committed before the worker saved the job outcome.
			linked_status = frappe.db.get_value("Flow Run", row.flow_run, "status") if row.flow_run else None
			if linked_status in ("Paused", "Completed", "Failed"):
				_agent_result(row.name, row.flow_run)
				continue
			# Queue waiting time is not execution time. A newly claimed job gets its full timeout.
			age = (now - get_datetime(row.started_at or row.reserved_at)).total_seconds()
			if status is not None or age > (row.timeout or 900) + 120:
				_finish(
					row.name,
					"Failed",
					error="Worker ended without a confirmed result. Review before resubmitting.",
				)

	# Paused rows are independent of the <=64 active slots. Only these need scan rotation.
	paused = frappe.get_all(
		"Flow Job",
		filters={"status": "Paused"},
		fields=["name"],
		order_by="modified asc",
		limit_page_length=RECONCILE_LIMIT,
	)
	for item in paused:
		row = frappe.get_doc("Flow Job", item.name, for_update=True)
		if row.status != "Paused":
			continue
		status = frappe.db.get_value("Flow Run", row.flow_run, "status") if row.flow_run else None
		if status in ("Completed", "Failed"):
			_finish(row.name, status, error="Linked agent run failed." if status == "Failed" else None)
		elif status is None:
			_finish(row.name, "Failed", error="Linked agent run no longer exists; review manually.")
		else:
			_update(row.name, {"status": "Paused"})


def _database_retries(function):
	"""Retry ONLY bookkeeping, never an AI call or agent/tool execution."""

	@wraps(function)
	def wrapped(*args, **kwargs):
		for attempt in range(3):
			try:
				return function(*args, **kwargs)
			except frappe.QueryDeadlockError:
				frappe.db.rollback()
				if attempt == 2:
					raise

	return wrapped


def _owns_reservation(name, reservation):
	row = frappe.get_doc("Flow Job", name, for_update=True)
	return row.status == "Running" and row.reservation == reservation


@_database_retries
def _persist(action, *, name, reservation):
	# An expired/recovered worker cannot overwrite a newer, already reconciled outcome.
	if _owns_reservation(name, reservation):
		action()
	frappe.db.commit()


@_database_retries
def dispatch():
	"""Scheduled entry point: commit bounded reservations, then publish to RQ. Not for request hooks."""
	# Begin with a fresh snapshot before acquiring the singleton lock; preceding worker/framework
	# reads must not hide another dispatcher's committed reservations under snapshot isolation.
	frappe.db.commit()
	try:
		settings = _settings_lock()
		now = now_datetime()
		_reconcile(now)
		reservations = []
		if settings.dispatch_enabled:
			active = frappe.db.count("Flow Job", {"status": ["in", ACTIVE]})
			available = max(0, settings.max_in_flight - active)
			if available:
				pending = frappe.get_all(
					"Flow Job",
					filters={"status": "Pending", "next_attempt_at": ["<=", now]},
					fields=["name"],
					order_by="creation asc",
					limit_page_length=available,
				)
				for item in pending:
					row = frappe.get_doc("Flow Job", item.name, for_update=True)
					if row.status != "Pending":
						continue
					token = uuid4().hex
					job_id = "flow-job-" + token
					_update(
						row.name,
						{
							"status": "Queued",
							"reservation": token,
							"rq_job_id": job_id,
							"reserved_at": now,
							"timeout": settings.job_timeout,
							"max_attempts": settings.decision_max_attempts
							if row.job_type == "Decision"
							else 1,
						},
					)
					reservations.append((row.name, token, job_id))
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		raise
	for name, token, job_id in reservations:
		try:
			frappe.enqueue(
				"flow.jobs.run",
				queue=settings.queue_name,
				timeout=settings.job_timeout,
				job_id=job_id,
				deduplicate=True,
				name=name,
				reservation=token,
			)
		except Exception:
			# Publication may actually have succeeded. Keep the token until reconciliation proves otherwise.
			frappe.log_error(title="Flow job publication failed; durable job retained")
			break
	return len(reservations)


@_database_retries
def _claim(name, reservation):
	row = frappe.get_doc("Flow Job", name, for_update=True)
	if row.status != "Queued" or row.reservation != reservation:
		frappe.db.rollback()
		return None
	_update(
		name,
		{
			"status": "Running",
			"started_at": now_datetime(),
			"attempts": row.attempts + 1,
			"error": None,
			"finished_at": None,
		},
	)
	frappe.db.commit()
	row.status = "Running"
	row.attempts += 1
	return row


def run(name: str, reservation: str):
	"""RQ-only worker entry. A stale/duplicate reservation never starts a second execution."""
	row = _claim(name, reservation)
	if row is None:
		return
	previous_user = frappe.session.user
	previous_job = frappe.flags.get("flow_background_job")
	previous_trigger = frappe.flags.get("flow_job_trigger")
	try:
		frappe.set_user(_identity(row.run_as))
		frappe.flags.flow_background_job = row.name
		payload = json.loads(row.payload)
		if row.job_type == "Decision":
			from flow.decisions import evaluate

			result = evaluate(**payload)
			outcome = partial(_finish, row.name, "Completed", result=_json(result))
		elif row.job_type == "Agent":
			agent = frappe.get_doc("Flow Agent", payload["agent"])
			agent.check_permission("read")
			if not agent.enabled:
				raise ValueError("Flow Agent is disabled.")
			result = agent.run(payload["input"], auto_approve=payload["auto_approve"], source="Trigger")
			outcome = partial(_agent_result, row.name, result.name)
		elif row.job_type == "Trigger":
			from flow.triggers import fire

			frappe.flags.flow_job_trigger = (
				payload["trigger"],
				payload["target_doctype"],
				payload["target_name"],
			)
			result = fire(**payload, expected_run_as=row.run_as)
			if result:
				outcome = partial(_agent_result, row.name, result)
			else:
				outcome = partial(
					_finish,
					row.name,
					"Cancelled",
					error="Trigger disabled, condition no longer met, or document missing.",
				)
		else:
			raise ValueError("Unsupported Flow job type.")
		# Commit successful agent/tool writes once. Only the job outcome transaction may be retried.
		frappe.db.commit()
		_persist(outcome, name=row.name, reservation=reservation)
	except Exception as exc:
		frappe.db.rollback()
		from flow.decisions import DecisionError

		# Only confirmed transient errors from side-effect-free decisions are retryable.
		if (
			row.job_type == "Decision"
			and isinstance(exc, DecisionError)
			and exc.kind in TRANSIENT
			and row.attempts < row.max_attempts
		):
			values = {
				"status": "Pending",
				"next_attempt_at": now_datetime() + timedelta(seconds=retry_delay(row.attempts)),
				"reservation": None,
				"rq_job_id": None,
				"error": str(exc),
			}
			_persist(lambda: _update(row.name, values), name=row.name, reservation=reservation)
		else:
			message = (
				str(exc)
				if isinstance(exc, DecisionError)
				else "Execution failed. Inspect the linked run where available; review before resubmitting."
			)
			_persist(
				lambda: _finish(row.name, "Failed", error=message), name=row.name, reservation=reservation
			)
	finally:
		frappe.flags.flow_background_job = previous_job
		frappe.flags.flow_job_trigger = previous_trigger
		frappe.set_user(previous_user)
		# Refill on completion rather than waiting a minute for every four jobs.
		# The periodic dispatcher remains the recovery path if Redis/DB is unavailable here.
		try:
			dispatch()
		except Exception:
			frappe.log_error(title="Flow queue refill deferred to scheduler")


def retry_delay(attempt: int) -> int:
	return min(3600, 30 * 2 ** min(max(attempt - 1, 0), 7))


def _agent_result(job_name, run_name):
	status = frappe.db.get_value("Flow Run", run_name, "status")
	if status == "Paused":
		_update(job_name, {"status": "Paused", "flow_run": run_name, "reservation": None, "rq_job_id": None})
	elif status == "Completed":
		_finish(job_name, "Completed", flow_run=run_name)
	else:
		_finish(job_name, "Failed", flow_run=run_name, error="Agent run did not complete.")


def attach_run(run_name):
	"""Called while creating a Flow Run, before agent.chat commits or invokes external tools."""
	job = frappe.flags.get("flow_background_job")
	if job and not frappe.db.get_value("Flow Job", job, "flow_run"):
		_update(job, {"flow_run": run_name})


@contextmanager
def resume_context(run, session):
	"""Keep a background run's identity and recursion guard through approval and streaming."""
	if run.source != "Trigger":
		yield False
		return
	job = frappe.db.get_value(
		"Flow Job", {"flow_run": run.name}, ["name", "run_as", "job_type", "payload"], as_dict=True
	)
	if not job:
		yield False
		return
	from flow.lib.session import _assert_session_owner, assert_run_owner

	# Authorize the human caller before assuming the original execution identity.
	_assert_session_owner(session)
	assert_run_owner(run)
	previous_user = frappe.session.user
	previous_job = frappe.flags.get("flow_background_job")
	previous_trigger = frappe.flags.get("flow_job_trigger")
	try:
		frappe.set_user(_identity(job.run_as))
		agent = frappe.get_doc("Flow Agent", session.agent)
		agent.check_permission("read")
		if not agent.enabled:
			raise frappe.PermissionError("Flow Agent is disabled.")
		frappe.flags.flow_background_job = job.name
		if job.job_type == "Trigger":
			payload = json.loads(job.payload)
			trigger = frappe.get_doc("Flow Trigger", payload["trigger"])
			if not trigger.enabled or (trigger.run_as or trigger.owner) != job.run_as:
				raise frappe.PermissionError(
					"Trigger disabled or execution identity changed after registration."
				)
			frappe.flags.flow_job_trigger = (
				payload["trigger"],
				payload["target_doctype"],
				payload["target_name"],
			)
		# Rebuild the runtime under this identity as well, including model permission checks.
		from flow.lib.session import _resolve_existing_agent

		session._runtime, session._snapshot = _resolve_existing_agent(session, None)
		yield True
	finally:
		frappe.flags.flow_background_job = previous_job
		frappe.flags.flow_job_trigger = previous_trigger
		frappe.set_user(previous_user)


def cleanup():
	"""Bounded daily retention. Incomplete work and independent Flow Run history are untouched."""
	settings = frappe.get_doc("Flow Settings")
	removed = 0
	for statuses, days in (
		(["Completed", "Cancelled"], settings.retention_days),
		(["Failed"], settings.failed_retention_days),
	):
		if not days:
			continue
		rows = frappe.get_all(
			"Flow Job",
			filters={"status": ["in", statuses], "finished_at": ["<", now_datetime() - timedelta(days=days)]},
			pluck="name",
			order_by="finished_at asc",
			limit_page_length=1000,
		)
		for names in frappe.utils.create_batch(rows, 100):
			frappe.db.delete("Flow Job", {"name": ["in", names], "status": ["in", statuses]})
		removed += len(rows)
	return removed
