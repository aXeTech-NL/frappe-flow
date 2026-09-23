"""Permission-checked entry point for Frappe apps. Deliberately not whitelisted."""

from __future__ import annotations

from typing import Any

from flow.decisions.client import DecisionError, SystemOneClient
from flow.decisions.schema import parse_questions


def evaluate(
	decision: str | None = None,
	*,
	state: Any,
	provider: str | None = None,
	questions: dict[str, Any] | None = None,
	model_id: str | None = None,
) -> dict[str, Any]:
	"""Evaluate a saved Flow Decision, or inline questions using a named provider.

	Saved decisions delegate use of their configured provider to users with read access
	to that decision. Inline calls require read access to the provider itself. This
	function never loads application records, executes actions, or persists input.
	"""
	connection, questions, model_id = resolve_request(
		decision, provider=provider, questions=questions, model_id=model_id
	)
	client = SystemOneClient(
		endpoint_url=connection.endpoint_url,
		api_key=connection.get_password("api_key", raise_exception=False) or None,
		model_id=model_id,
		timeout=connection.timeout,
	)
	return client.evaluate(state=state, questions=questions)


def resolve_request(decision=None, *, provider=None, questions=None, model_id=None):
	"""Authorize a request without retrieving credentials or contacting the provider."""
	import frappe

	if frappe.session.user == "Guest":
		raise frappe.PermissionError("Sign in to evaluate a Flow Decision.")

	if decision is not None:
		if provider is not None or questions is not None or model_id is not None:
			raise ValueError("Pass either a saved decision or inline provider/questions, not both.")
		_require_name(decision, "Decision")
		doc = frappe.get_doc("Flow Decision", decision)
		doc.check_permission("read")
		if not doc.enabled:
			raise DecisionError("Flow Decision is disabled.", kind="configuration")
		provider = doc.provider
		questions = parse_questions(doc.questions)
		model_id = doc.model_id or None
	else:
		_require_name(provider, "Provider")
		if questions is None:
			raise ValueError("Questions are required for an inline decision.")

	connection = frappe.get_doc("Flow Decision Provider", provider)
	if decision is None:
		connection.check_permission("read")
	if not connection.enabled:
		raise DecisionError("Flow Decision Provider is disabled.", kind="configuration")

	return connection, questions, model_id if model_id is not None else connection.model_id or None


def submit(decision=None, *, state, provider=None, questions=None, model_id=None, idempotency_key=None):
	"""Register a background decision in the current transaction; return its Flow Job name."""
	from flow.jobs import submit_decision

	return submit_decision(
		decision,
		state=state,
		provider=provider,
		questions=questions,
		model_id=model_id,
		idempotency_key=idempotency_key,
	)


def _require_name(value: Any, label: str) -> None:
	if not isinstance(value, str) or not value.strip():
		raise ValueError(f"{label} must be a non-empty document name.")
