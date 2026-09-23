import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from flow.decisions import evaluate, noul, submit
from flow.flow.doctype.flow_decision_provider.test_flow_decision_provider import provider_doc


class TestFlowDecision(IntegrationTestCase):
	def setUp(self):
		self.provider = provider_doc().insert()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def make_decision(self, **overrides):
		return frappe.get_doc(
			{
				"doctype": "Flow Decision",
				"title": "Test triage",
				"slug": "test_triage_" + frappe.generate_hash(length=8).lower(),
				"provider": self.provider.name,
				"enabled": 1,
				"questions": json.dumps({"urgent": noul("Is this urgent?")}),
				**overrides,
			}
		)

	def test_definition_is_reusable(self):
		doc = self.make_decision().insert()
		with patch("flow.decisions.service.SystemOneClient") as client:
			client.return_value.evaluate.return_value = {"model": "custom-model", "answers": {}}
			doc.evaluate({"ticket": "one"})
			doc.evaluate({"ticket": "two"})
		self.assertEqual(client.return_value.evaluate.call_count, 2)
		self.assertEqual(client.return_value.evaluate.call_args.kwargs["state"], {"ticket": "two"})

	def test_invalid_slug_and_questions(self):
		for fields in ({"slug": "Bad Slug"}, {"questions": "{}"}, {"questions": '{"q":{"type":"chat"}}'}):
			with self.subTest(fields=fields), self.assertRaises(frappe.ValidationError):
				self.make_decision(**fields).insert()

	def test_unsaved_definition_cannot_be_evaluated(self):
		with self.assertRaises(frappe.ValidationError):
			self.make_decision().evaluate("test")

	def test_model_override_and_saved_questions(self):
		doc = self.make_decision(model_id="other-model").insert()
		doc.questions = json.dumps({"modified": noul("Do not use this unsaved question")})
		with patch("flow.decisions.service.SystemOneClient") as client:
			doc.evaluate("test")
		self.assertEqual(client.call_args.kwargs["model_id"], "other-model")
		self.assertEqual(set(client.return_value.evaluate.call_args.kwargs["questions"]), {"urgent"})

	def test_read_permission_delegates_definition_but_not_inline_provider_access(self):
		from frappe.share import add as share

		doc = self.make_decision().insert()
		email = f"system-one-{frappe.generate_hash(length=8)}@example.com"
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": "Decision Reader", "send_welcome_email": 0}
		).insert()
		share("Flow Decision", doc.name, user=email, read=1)
		frappe.set_user(email)
		with patch("flow.decisions.service.SystemOneClient") as client:
			evaluate(doc.name, state="test")
			client.assert_called_once()
			with self.assertRaises(frappe.PermissionError):
				evaluate(provider=self.provider.name, state="test", questions={"q": noul("Yes?")})

	def test_background_submit_persists_input_without_provider_call(self):
		from flow.jobs import get_job

		doc = self.make_decision().insert()
		with patch("flow.decisions.service.SystemOneClient") as client, patch("frappe.enqueue") as enqueue:
			name = submit(doc.name, state={"subject": "ticket"})
		client.assert_not_called()
		enqueue.assert_not_called()
		self.assertEqual(get_job(name)["status"], "Pending")
		payload = frappe.db.get_value("Flow Job", name, "payload")
		self.assertEqual(json.loads(payload)["state"], {"subject": "ticket"})
		self.assertNotIn("test-system-one-key", payload)

	def test_guest_cannot_evaluate(self):
		doc = self.make_decision().insert()
		frappe.set_user("Guest")
		with (
			patch("flow.decisions.service.SystemOneClient") as client,
			self.assertRaises(frappe.PermissionError),
		):
			evaluate(doc.name, state="test")
		client.assert_not_called()
