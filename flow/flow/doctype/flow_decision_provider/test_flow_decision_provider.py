from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from flow.decisions import DecisionError, evaluate, noul


def provider_doc(**overrides):
	return frappe.get_doc(
		{
			"doctype": "Flow Decision Provider",
			"provider_name": "Test System One " + frappe.generate_hash(length=8),
			"endpoint_url": "http://localhost:8000/v1/systemone",
			"api_key": "test-system-one-key",
			"model_id": "custom-model",
			"timeout": 30,
			"enabled": 1,
			**overrides,
		}
	)


class TestFlowDecisionProvider(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_custom_connections_do_not_require_a_known_vendor(self):
		first = provider_doc().insert()
		second = provider_doc().insert()
		self.assertNotEqual(first.name, second.name)
		self.assertEqual(first.endpoint_url, second.endpoint_url)
		self.assertEqual(first.get_password("api_key"), "test-system-one-key")

	def test_optional_key_and_model(self):
		doc = provider_doc(api_key=None, model_id=None).insert()
		self.assertFalse(doc.get_password("api_key", raise_exception=False))

	def test_invalid_config_rejected(self):
		for fields in (
			{"endpoint_url": "file:///tmp/test"},
			{"endpoint_url": "https://key:secret@server/v1"},
			{"timeout": 0},
			{"timeout": 301},
		):
			with self.subTest(fields=fields), self.assertRaises(frappe.ValidationError):
				provider_doc(**fields).insert()

	def test_invalid_bearer_tokens_are_rejected_on_save(self):
		for key in ("secret key", "secret\u20ac", "secret\x7f"):
			with self.subTest(key=key), self.assertRaises(frappe.ValidationError) as caught:
				provider_doc(api_key=key).insert()
			self.assertNotIn(key, str(caught.exception))

	def test_connection_test_uses_saved_settings_not_submitted_endpoint(self):
		doc = provider_doc().insert()
		doc.endpoint_url = "https://do-not-send.example/"
		doc.api_key = "do-not-use"
		with patch("flow.decisions.service.SystemOneClient") as client:
			client.return_value.evaluate.return_value = {"model": "actual-model", "answers": {}}
			self.assertTrue(doc.test_connection()["ok"])
		self.assertEqual(client.call_args.kwargs["endpoint_url"], "http://localhost:8000/v1/systemone")
		self.assertEqual(client.call_args.kwargs["api_key"], "test-system-one-key")

	def test_disabled_provider_fails_before_network(self):
		doc = provider_doc(enabled=0).insert()
		with patch("flow.decisions.service.SystemOneClient") as client, self.assertRaises(DecisionError):
			evaluate(provider=doc.name, state="test", questions={"q": noul("Yes?")})
		client.assert_not_called()

	def test_unsaved_provider_cannot_be_tested(self):
		with self.assertRaises(frappe.ValidationError):
			provider_doc().test_connection()
