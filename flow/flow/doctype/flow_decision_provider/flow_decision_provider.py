from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from flow.decisions import DecisionError, evaluate, noul
from flow.decisions.client import validate_api_key, validate_connection


class FlowDecisionProvider(Document):
	def validate(self):
		for field in ("provider_name", "endpoint_url", "model_id", "api_key"):
			value = self.get(field)
			if isinstance(value, str):
				self.set(field, value.strip())
		try:
			validate_connection(self.endpoint_url, self.model_id or None, self.timeout)
			validate_api_key(self.api_key)
		except ValueError as exc:
			frappe.throw(str(exc), title=_("Invalid System One Connection"))

	@frappe.whitelist()
	def test_connection(self):
		# Resolve persisted settings: submitted document fields must not override endpoint/credentials.
		if self.is_new() or not self.name:
			frappe.throw(_("Save the provider before testing the connection."))
		saved = frappe.get_doc("Flow Decision Provider", self.name)
		saved.check_permission("write")
		try:
			result = evaluate(
				provider=saved.name,
				state="Flow System One connection test.",
				questions={"connection": noul("Does the text mention Flow?")},
			)
		except (DecisionError, ValueError) as exc:
			frappe.throw(str(exc), title=_("System One Connection Failed"))
		return {"ok": True, "model": result["model"], "message": _("Connection OK")}
