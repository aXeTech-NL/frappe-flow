import frappe
from frappe.model.document import Document


class FlowJob(Document):
	def validate(self):
		if not self.flags.get("flow_job_internal"):
			raise frappe.PermissionError("Flow Jobs are managed by the queue; use the submission APIs.")

	def before_rename(self, *args, **kwargs):
		raise frappe.PermissionError("Flow Job identifiers cannot be changed.")
