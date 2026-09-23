import frappe
from frappe.tests import IntegrationTestCase


class TestFlowSettings(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_invalid_limits_are_rejected(self):
		for field, value in (
			("max_in_flight", 0),
			("max_in_flight", 65),
			("job_timeout", 59),
			("decision_max_attempts", 11),
			("retention_days", -1),
			("failed_retention_days", -1),
		):
			with self.subTest(field=field, value=value):
				doc = frappe.get_doc("Flow Settings")
				doc.set(field, value)
				with self.assertRaises(frappe.ValidationError):
					doc.validate()

	def test_zero_retention_is_allowed(self):
		doc = frappe.get_doc("Flow Settings")
		doc.retention_days = 0
		doc.failed_retention_days = 0
		doc.validate()

	def test_queue_name_is_not_a_path(self):
		doc = frappe.get_doc("Flow Settings")
		doc.queue_name = "../queue"
		with self.assertRaises(frappe.ValidationError):
			doc.validate()
