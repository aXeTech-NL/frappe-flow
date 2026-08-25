# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

import json
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.model.document import Document

# Keys LiteLLM derives itself or that have dedicated fields.
RESERVED_PARAM_KEYS = frozenset(
	{
		"model",
		"api_key",
		"api_base",
		"base_url",
		"messages",
		"input",
		"stream",
		"tools",
		"tool_choice",
		"extra_headers",
		"extra_query",
		"extra_body",
	}
)
RESERVED_REQUEST_KEYS = frozenset(
	{"model", "api_key", "api_base", "base_url", "messages", "input", "stream", "tools", "tool_choice"}
)
RESERVED_HEADER_KEYS = frozenset({"authorization", "api-key", "x-api-key"})
CUSTOM_CONNECTOR = "custom"
OPENAI_CONNECTOR = "openai"
LEGACY_OPENAI_LIKE_CONNECTOR = "openai_like"
API_STYLES = frozenset({"Auto", "Responses", "Chat Completions"})


def connector_id(value: str | None) -> str:
	"""Return the LiteLLM connector represented by a stored Connector value.

	Custom connections speak the OpenAI protocol at a user-supplied Base URL.
	LiteLLM 1.83's generic ``openai_like`` slug is not mapped for chat calls.
	"""
	value = (value or "").strip().lower()
	return OPENAI_CONNECTOR if value == CUSTOM_CONNECTOR else value


def strip_connector_prefix(model_id: str, connector: str) -> str:
	"""Return the provider-relative model ID shown and stored by Flow.

	Linked Flow Models historically stored the internal LiteLLM connector prefix.
	The Provider link now carries that routing information, so remove only the
	expected connector (plus v16.3.0's legacy Custom prefix).
	"""
	prefixes = [connector]
	if connector == OPENAI_CONNECTOR:
		prefixes.append(LEGACY_OPENAI_LIKE_CONNECTOR)
	for prefix in prefixes:
		if model_id.startswith(f"{prefix}/"):
			return model_id.removeprefix(f"{prefix}/")
	return model_id


def compose_model_id(model_id: str, connector: str) -> str:
	"""Build the internal LiteLLM ID without changing the remote model name."""
	return f"{connector}/{strip_connector_prefix(model_id, connector)}"


def connector_ids() -> set[str]:
	"""Known LiteLLM connector prefixes, including the legacy Custom prefix."""
	try:
		import litellm
	except ImportError:
		return {OPENAI_CONNECTOR, LEGACY_OPENAI_LIKE_CONNECTOR}
	return {provider.value for provider in litellm.provider_list} | {LEGACY_OPENAI_LIKE_CONNECTOR}


def known_connectors() -> list[str]:
	return ["Custom", *sorted(connector_ids() - {LEGACY_OPENAI_LIKE_CONNECTOR})]


def parse_json_object(value: str | None, label: str) -> dict:
	if not value:
		return {}
	try:
		parsed = json.loads(value)
	except (TypeError, ValueError):
		frappe.throw(_("{0} must be valid JSON.").format(label), title=_("Invalid JSON"))
	if not isinstance(parsed, dict):
		frappe.throw(_("{0} must be a JSON object.").format(label), title=_("Invalid JSON"))
	return parsed


def validate_request_settings(fieldname: str, value: str | None, label: str) -> None:
	parsed = parse_json_object(value, label)
	reserved = RESERVED_HEADER_KEYS if fieldname == "extra_headers" else RESERVED_REQUEST_KEYS
	keys = {str(key).lower() for key in parsed}
	conflicting = sorted(reserved.intersection(keys))
	if conflicting:
		frappe.throw(
			_("{0} may not include reserved keys: {1}.").format(label, ", ".join(conflicting)),
			title=_("Reserved Settings"),
		)


class FlowProvider(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Password | None
		api_style: DF.Literal["Auto", "Chat Completions", "Responses"]
		base_url: DF.Data | None
		chat_base_url: DF.Data | None
		embedding_base_url: DF.Data | None
		enabled: DF.Check
		extra_body: DF.JSON | None
		extra_headers: DF.JSON | None
		extra_params: DF.JSON | None
		extra_query: DF.JSON | None
		provider: DF.Data
		responses_base_url: DF.Data | None
		title: DF.Data
	# end: auto-generated types

	def autoname(self):
		# Naming runs before validation, so normalize here as well. Existing documents
		# keep their current names; only new connections use their free-form title.
		self._normalize()
		self.name = self.title

	def validate(self):
		self._normalize()
		self._validate_provider_known()
		self._validate_urls()
		self._validate_json_settings()
		if (self.api_style or "Auto") not in API_STYLES:
			frappe.throw(_("Invalid default API Style."), title=_("Invalid API Style"))

	def _normalize(self):
		self.title = (self.title or "").strip()
		self.provider = (self.provider or "").strip().lower()
		for fieldname in ("base_url", "chat_base_url", "responses_base_url", "embedding_base_url"):
			value = self.get(fieldname)
			if isinstance(value, str):
				self.set(fieldname, value.strip())
		if isinstance(self.api_key, str):
			self.api_key = self.api_key.strip()

	def _validate_provider_known(self):
		if self.provider == CUSTOM_CONNECTOR:
			return
		try:
			import litellm
		except ImportError:
			return
		known = {provider.value for provider in litellm.provider_list}
		if self.provider not in known:
			frappe.throw(
				_("Unknown connector {0}. Choose a LiteLLM connector or Custom.").format(
					frappe.bold(self.provider)
				),
				title=_("Invalid Connector"),
			)

	def _validate_urls(self):
		url_fields = ("base_url", "chat_base_url", "responses_base_url", "embedding_base_url")
		if self.provider == CUSTOM_CONNECTOR and not any(self.get(fieldname) for fieldname in url_fields):
			frappe.throw(
				_("At least one Base URL is required for a Custom OpenAI-compatible connection."),
				title=_("Base URL Required"),
			)
		for fieldname in url_fields:
			value = self.get(fieldname)
			if not value:
				continue
			parsed = urlparse(value)
			if parsed.scheme not in ("http", "https") or not parsed.netloc:
				frappe.throw(
					_("{0} must be an absolute http(s) URL.").format(self.meta.get_label(fieldname)),
					title=_("Invalid Base URL"),
				)

	def _validate_json_settings(self):
		extra_params = parse_json_object(self.extra_params, _("Extra Params"))
		conflicting = sorted(RESERVED_PARAM_KEYS.intersection(extra_params))
		if conflicting:
			frappe.throw(
				_("Extra Params may not include reserved keys: {0}.").format(", ".join(conflicting)),
				title=_("Reserved Params"),
			)
		for fieldname, label in (
			("extra_headers", _("Extra Headers")),
			("extra_query", _("Extra Query")),
			("extra_body", _("Extra Body")),
		):
			validate_request_settings(fieldname, self.get(fieldname), label)


@frappe.whitelist()
def get_connectors() -> list[str]:
	return known_connectors()
