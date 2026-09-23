from __future__ import annotations

import re
from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document

from flow.decisions.schema import parse_questions


class FlowDecision(Document):
	def validate(self):
		for field in ("title", "slug", "model_id"):
			value = self.get(field)
			if isinstance(value, str):
				self.set(field, value.strip())
		if not re.fullmatch(r"[a-z][a-z0-9_]*", self.slug or ""):
			frappe.throw(_("Slug must be snake_case, starting with a lowercase letter."))
		# Creating/editing a definition delegates this connection to its future readers.
		if self.provider:
			frappe.get_doc("Flow Decision Provider", self.provider).check_permission("read")
		try:
			parse_questions(self.questions)
		except (ValueError, RecursionError):
			frappe.throw(_("Questions must be valid System One question definitions."))

	def evaluate(self, state: Any) -> dict[str, Any]:
		"""Evaluate the saved definition with the current user's permissions."""
		from flow.decisions import evaluate

		if self.is_new():
			frappe.throw(_("Save the decision before evaluating it."))
		return evaluate(self.name, state=state)
