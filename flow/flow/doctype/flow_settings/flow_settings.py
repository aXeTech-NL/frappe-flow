import re

import frappe
from frappe.model.document import Document


class FlowSettings(Document):
	def validate(self):
		self.queue_name = (self.queue_name or "").strip()
		if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", self.queue_name):
			frappe.throw("Worker Queue must be a lowercase queue name (letters, digits, _ or -).")
		for field, minimum, maximum in (
			("max_in_flight", 1, 64),
			("job_timeout", 60, 7200),
			("decision_max_attempts", 1, 10),
			("retention_days", 0, 3650),
			("failed_retention_days", 0, 3650),
		):
			value = self.get(field)
			if type(value) is not int or not minimum <= value <= maximum:
				frappe.throw(f"{self.meta.get_label(field)} must be between {minimum} and {maximum}.")
