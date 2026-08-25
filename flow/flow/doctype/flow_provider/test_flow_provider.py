# Copyright (c) 2026, Frappe Technologies and Contributors
# See license.txt

from typing import Any
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from flow.flow.doctype.flow_provider.flow_provider import connector_id
from flow.lib.model import Model, resolve_provider_credentials


def _provider(**overrides: Any) -> dict:
	doc = {
		"doctype": "Flow Provider",
		"title": "OpenRouter Test Connection",
		"provider": "openrouter",
		"api_key": "sk-provider",
		"enabled": 1,
	}
	doc.update(overrides)
	return doc


def _model(**overrides: Any) -> dict:
	doc = {
		"doctype": "Flow Model",
		"title": "Test Provider Model",
		"model_id": "openrouter/test/model",
		"enabled": 1,
	}
	doc.update(overrides)
	return doc


class TestFlowProviderValidation(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_title_names_new_connection(self):
		doc = frappe.get_doc(_provider(title="  OpenRouter Test Connection  ")).insert()
		self.assertEqual(doc.name, "OpenRouter Test Connection")
		self.assertEqual(doc.title, doc.name)

	def test_connector_normalized_lowercase(self):
		doc = frappe.get_doc(_provider(provider="OpenRouter")).insert()
		self.assertEqual(doc.provider, "openrouter")

	def test_duplicate_connector_connections_allowed(self):
		first = frappe.get_doc(_provider()).insert()
		second = frappe.get_doc(_provider(title="Backup OpenRouter")).insert()
		self.assertNotEqual(first.name, second.name)
		self.assertEqual(first.provider, second.provider)

	def test_unknown_connector_rejected(self):
		doc = frappe.get_doc(_provider(provider="not-a-real-provider"))
		with self.assertRaisesRegex(frappe.ValidationError, "Unknown connector"):
			doc.insert()

	def test_title_patch_preserves_legacy_name_and_model_link(self):
		from flow.patches.backfill_flow_provider_titles import execute

		connection = frappe.get_doc(_provider(title="Legacy Connection")).insert()
		legacy_name = frappe.rename_doc("Flow Provider", connection.name, "openrouter", force=True)
		model = frappe.get_doc(_model(provider=legacy_name, model_id="test/model")).insert()
		frappe.db.set_value("Flow Provider", legacy_name, "title", "", update_modified=False)

		execute()

		self.assertEqual(frappe.db.get_value("Flow Provider", legacy_name, "title"), "openrouter")
		self.assertEqual(frappe.db.get_value("Flow Model", model.name, "provider"), legacy_name)

	def test_custom_is_openai_compatible_and_requires_a_base_url(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Base URL is required"):
			frappe.get_doc(_provider(provider="Custom")).insert()
		doc = frappe.get_doc(
			_provider(provider="Custom", responses_base_url="https://custom.example.com/v1")
		).insert()
		self.assertEqual(doc.provider, "custom")
		self.assertEqual(connector_id(doc.provider), "openai")

	def test_invalid_base_url_rejected(self):
		doc = frappe.get_doc(_provider(base_url="not-a-url"))
		with self.assertRaisesRegex(frappe.ValidationError, "Base URL"):
			doc.insert()

	def test_extra_params_reserved_key_rejected(self):
		doc = frappe.get_doc(_provider(extra_params='{"api_key": "x"}'))
		with self.assertRaisesRegex(frappe.ValidationError, "reserved"):
			doc.insert()

	def test_extra_params_invalid_json_rejected(self):
		doc = frappe.get_doc(_provider(extra_params="{not json"))
		with self.assertRaisesRegex(frappe.ValidationError, "valid JSON"):
			doc.insert()

	def test_operation_base_urls_are_validated(self):
		for fieldname in ("chat_base_url", "responses_base_url", "embedding_base_url"):
			with self.subTest(fieldname=fieldname):
				with self.assertRaisesRegex(frappe.ValidationError, "absolute http"):
					frappe.get_doc(_provider(**{fieldname: "not-a-url"})).insert()

	def test_advanced_json_settings_accept_safe_objects(self):
		doc = frappe.get_doc(
			_provider(
				extra_headers='{"X-Tenant": "acme"}',
				extra_query='{"api-version": "2026-01-01"}',
				extra_body='{"metadata": {"source": "flow"}}',
			)
		).insert()
		self.assertEqual(doc.api_style, "Auto")

	def test_advanced_json_settings_reject_reserved_keys(self):
		for fieldname, value in (
			("extra_headers", '{"Authorization": "Bearer secret"}'),
			("extra_query", '{"api_key": "secret"}'),
			("extra_body", '{"messages": []}'),
		):
			with self.subTest(fieldname=fieldname):
				with self.assertRaisesRegex(frappe.ValidationError, "reserved keys"):
					frappe.get_doc(_provider(**{fieldname: value})).insert()


class TestProviderCredentialResolution(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_model_resolves_exact_linked_connection(self):
		connection = frappe.get_doc(_provider(api_key="sk-from-provider")).insert()
		model_doc = frappe.get_doc(_model(provider=connection.name, model_id="test/model")).insert()
		self.assertEqual(Model(model_doc.name)._api_key, "sk-from-provider")

	def test_model_own_key_overrides_provider(self):
		connection = frappe.get_doc(_provider(api_key="sk-from-provider")).insert()
		model_doc = frappe.get_doc(
			_model(provider=connection.name, model_id="test/model", api_key="sk-on-model")
		).insert()
		self.assertEqual(Model(model_doc.name)._api_key, "sk-on-model")

	def test_duplicate_connectors_keep_linked_credentials_separate(self):
		first = frappe.get_doc(_provider(api_key="sk-first")).insert()
		second = frappe.get_doc(_provider(title="Second OpenRouter", api_key="sk-second")).insert()
		first_model = frappe.get_doc(
			_model(provider=first.name, model_id="test/model", title="First Model")
		).insert()
		second_model = frappe.get_doc(
			_model(provider=second.name, model_id="test/model", title="Second Model")
		).insert()
		self.assertEqual(Model(first_model.name)._api_key, "sk-first")
		self.assertEqual(Model(second_model.name)._api_key, "sk-second")

	def test_disabled_linked_connection_not_used(self):
		connection = frappe.get_doc(_provider(api_key="sk-provider", enabled=0)).insert()
		model_doc = frappe.get_doc(_model(provider=connection.name, model_id="test/model")).insert()
		self.assertIsNone(Model(model_doc.name)._api_key)

	def test_legacy_implicit_lookup_requires_exactly_one_match(self):
		frappe.get_doc(_provider(api_key="sk-only")).insert()
		self.assertEqual(resolve_provider_credentials("openrouter/test")["api_key"], "sk-only")
		frappe.get_doc(_provider(title="Duplicate OpenRouter", api_key="sk-other")).insert()
		self.assertEqual(resolve_provider_credentials("openrouter/test"), {})

	def test_provider_base_url_used_when_model_has_none(self):
		connection = frappe.get_doc(_provider(base_url="http://gateway.local")).insert()
		model_doc = frappe.get_doc(_model(provider=connection.name, model_id="test/model")).insert()
		self.assertEqual(Model(model_doc.name).base_url, "http://gateway.local")

	def test_provider_extra_params_merged_model_wins(self):
		connection = frappe.get_doc(
			_provider(extra_params='{"api_version": "v1", "temperature": 0.1}')
		).insert()
		model_doc = frappe.get_doc(
			_model(provider=connection.name, model_id="test/model", params='{"temperature": 0.9}')
		).insert()
		model = Model(model_doc.name)
		self.assertEqual(model.params["api_version"], "v1")
		self.assertEqual(model.params["temperature"], 0.9)


class TestProviderTitlePatch(UnitTestCase):
	def test_patch_backfills_only_blank_titles_and_is_repeatable(self):
		from flow.patches.backfill_flow_provider_titles import execute

		with (
			patch.object(frappe.db, "table_exists", return_value=True),
			patch.object(frappe.db, "has_column", return_value=True),
			patch.object(frappe.db, "sql") as sql,
		):
			execute()
			execute()

		self.assertEqual(sql.call_count, 2)
		query = sql.call_args.args[0]
		self.assertIn("SET title = provider", query)
		self.assertIn("COALESCE(title, '') = ''", query)

	def test_patch_is_safe_before_column_exists(self):
		from flow.patches.backfill_flow_provider_titles import execute

		with (
			patch.object(frappe.db, "table_exists", return_value=True),
			patch.object(frappe.db, "has_column", return_value=False),
			patch.object(frappe.db, "sql") as sql,
		):
			execute()

		sql.assert_not_called()
