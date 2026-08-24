# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

from __future__ import annotations

import json
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any

import frappe

DEFAULT_TIMEOUT = 60
API_STYLE_PROVIDER_DEFAULT = "Provider Default"
API_STYLE_AUTO = "Auto"
API_STYLE_RESPONSES = "Responses"
API_STYLE_CHAT_COMPLETIONS = "Chat Completions"
API_STYLES = frozenset(
	{API_STYLE_PROVIDER_DEFAULT, API_STYLE_AUTO, API_STYLE_RESPONSES, API_STYLE_CHAT_COMPLETIONS}
)
REQUEST_SETTING_KEYS = ("extra_headers", "extra_query", "extra_body")
RESERVED_PARAM_KEYS = frozenset(
	{"model", "api_key", "api_base", "base_url", "messages", "input", "stream", "tools", "tool_choice"}
)
RESERVED_HEADER_KEYS = frozenset({"authorization", "api-key", "x-api-key"})


@dataclass
class ToolCall:
	id: str
	name: str
	arguments: dict[str, Any]
	# Set when the model emitted unparseable arguments; the agent feeds this back so it can retry
	# instead of the whole run failing.
	error: str | None = None


@dataclass
class ToolCallBegin:
	"""Streamed marker: the model has started emitting a tool call (name known, arguments still
	streaming). Lets the UI show the tool immediately instead of after the full arguments arrive."""

	id: str
	name: str


@dataclass
class ChatResponse:
	content: str | None
	tool_calls: list[ToolCall] = field(default_factory=list)
	finish_reason: str | None = None
	usage: dict[str, int] = field(default_factory=dict)


class Model:
	def __init__(
		self,
		name: str | None = None,
		*,
		model_id: str | None = None,
		api_key: str | None = None,
		base_url: str | None = None,
		params: dict[str, Any] | None = None,
		timeout: int = DEFAULT_TIMEOUT,
		api_style: str | None = None,
	):
		connection_name = None
		model_base_url = base_url
		if name is not None:
			if model_id or api_key or base_url or params or api_style is not None:
				raise ValueError("Pass either a Flow Model doc name or explicit kwargs, not both.")
			doc = frappe.get_doc("Flow Model", name)
			if not doc.enabled:
				raise ValueError(f"Flow Model {name!r} is disabled")
			model_id = doc.model_id
			api_key = doc.get_password("api_key", raise_exception=False)
			model_base_url = doc.base_url or None
			params = json.loads(doc.params) if doc.params else {}
			api_style = getattr(doc, "api_style", None) or API_STYLE_AUTO
			connection_name = getattr(doc, "provider", None) or None

		if not model_id:
			raise ValueError("model_id is required")
		api_style = api_style or API_STYLE_AUTO
		if api_style not in API_STYLES:
			raise ValueError(f"api_style must be one of {sorted(API_STYLES)}, got {api_style!r}")

		# Model settings override the exact linked connection. Legacy unlinked models
		# resolve only when one enabled connection matches their connector.
		provider_config = resolve_provider_credentials(model_id, connection_name)
		api_key = api_key or provider_config.get("api_key")
		if api_style == API_STYLE_PROVIDER_DEFAULT:
			api_style = provider_config.get("api_style") or API_STYLE_AUTO
		provider_params = provider_config.get("extra_params") or {}
		for key in REQUEST_SETTING_KEYS:
			if provider_config.get(key):
				provider_params[key] = provider_config[key]
		params = _merge_params(provider_params, params or {})
		_validate_runtime_params(params)

		# A generic/model base marks OpenAI Auto as a proxy. Operation-specific
		# endpoints are selected only after deciding the effective API operation.
		routing_base_url = model_base_url or provider_config.get("base_url")
		routed_model_id = route_model_id(model_id, api_style, routing_base_url)
		operation = "responses" if "/responses/" in routed_model_id else "chat"
		operation_base_url = provider_config.get(f"{operation}_base_url")

		self.model_id = model_id
		self.routed_model_id = routed_model_id
		self._api_key = api_key or None
		self.base_url = model_base_url or operation_base_url or provider_config.get("base_url")
		self.params = params
		self.timeout = timeout
		self.api_style = api_style

	def chat(
		self,
		messages: str | list[dict[str, Any]],
		tools: list[dict[str, Any]] | None = None,
		*,
		stream: bool = False,
	) -> ChatResponse | Generator[str, None, ChatResponse]:
		"""Call the model. When `stream=True`, returns a generator that yields text deltas
		and returns the assembled `ChatResponse` via PEP 380 (`StopIteration.value`)."""
		import litellm

		if isinstance(messages, str):
			messages = [{"role": "user", "content": messages}]

		kwargs = self.completion_kwargs(messages, tools=tools, stream=stream)
		if stream:
			return _consume_stream(litellm.completion(**kwargs))
		return _normalize(litellm.completion(**kwargs))

	def completion_kwargs(
		self,
		messages: list[dict[str, Any]],
		*,
		tools: list[dict[str, Any]] | None = None,
		stream: bool = False,
	) -> dict[str, Any]:
		kwargs: dict[str, Any] = {
			"model": self.routed_model_id,
			"api_key": self._api_key,
			"messages": messages,
			"timeout": self.timeout,
			**self.params,
		}
		if self.base_url:
			kwargs["api_base"] = self.base_url
		if tools:
			kwargs["tools"] = tools
		if stream:
			kwargs["stream"] = True
			kwargs["stream_options"] = {"include_usage": True}
		return kwargs


def route_model_id(model_id: str, api_style: str | None = None, base_url: str | None = None) -> str:
	"""Return the LiteLLM model ID for the selected OpenAI API style.

	LiteLLM's completion compatibility layer selects the Responses API when the
	OpenAI provider-relative model starts with ``responses/``. Other providers,
	and custom endpoints in Auto mode, retain LiteLLM's default routing.
	"""
	api_style = api_style or API_STYLE_AUTO
	if api_style not in API_STYLES:
		raise ValueError(f"api_style must be one of {sorted(API_STYLES)}, got {api_style!r}")
	if api_style == API_STYLE_PROVIDER_DEFAULT:
		api_style = API_STYLE_AUTO

	connector = next(
		(prefix for prefix in ("openai", "openai_like") if model_id.startswith(f"{prefix}/")),
		None,
	)
	if not connector:
		return model_id

	provider_model = model_id.removeprefix(f"{connector}/")
	is_responses_model = provider_model.startswith("responses/")
	use_responses = api_style == API_STYLE_RESPONSES or (
		api_style == API_STYLE_AUTO and connector == "openai" and not base_url
	)

	if use_responses:
		# LiteLLM exposes Responses through its OpenAI bridge, including for an
		# openai_like model with a custom operation endpoint.
		response_model = provider_model.removeprefix("responses/")
		return f"openai/responses/{response_model}"
	if api_style == API_STYLE_CHAT_COMPLETIONS and is_responses_model:
		return f"{connector}/{provider_model.removeprefix('responses/')}"
	return model_id


def resolve_provider_credentials(model_id: str, connection_name: str | None = None) -> dict[str, Any]:
	"""Resolve one enabled Flow Provider connection.

	A linked Flow Model always addresses its exact connection. Unlinked legacy
	models retain implicit lookup only when one enabled connection matches the
	LiteLLM connector; duplicate matches intentionally return no credentials.
	"""
	if connection_name:
		try:
			doc = frappe.get_doc("Flow Provider", connection_name)
		except Exception:
			return {}
		return _provider_credentials(doc) if doc.enabled else {}

	try:
		import litellm

		provider = litellm.get_llm_provider(model_id)[1]
		from flow.flow.doctype.flow_provider.flow_provider import connector_id

		rows = frappe.get_all("Flow Provider", filters={"enabled": 1}, fields=["name", "provider"])
		matches = [row.name for row in rows if connector_id(row.provider) == provider]
	except Exception:
		return {}

	if len(matches) != 1:
		return {}
	return _provider_credentials(frappe.get_doc("Flow Provider", matches[0]))


def _provider_credentials(doc: Any) -> dict[str, Any]:
	config = {
		"api_key": doc.get_password("api_key", raise_exception=False) or None,
		"api_style": getattr(doc, "api_style", None) or API_STYLE_AUTO,
		"base_url": getattr(doc, "base_url", None) or None,
		"chat_base_url": getattr(doc, "chat_base_url", None) or None,
		"responses_base_url": getattr(doc, "responses_base_url", None) or None,
		"embedding_base_url": getattr(doc, "embedding_base_url", None) or None,
		"extra_params": _json_object(getattr(doc, "extra_params", None)),
	}
	for key in REQUEST_SETTING_KEYS:
		config[key] = _json_object(getattr(doc, key, None))
	return config


def _json_object(value: str | None) -> dict[str, Any]:
	return json.loads(value) if value else {}


def _merge_params(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
	"""Merge model params over provider defaults, preserving nested request settings."""
	merged = {**defaults, **overrides}
	for key in REQUEST_SETTING_KEYS:
		if key in defaults or key in overrides:
			merged[key] = {**(defaults.get(key) or {}), **(overrides.get(key) or {})}
	return merged


def _validate_runtime_params(params: dict[str, Any]) -> None:
	conflicting = sorted(RESERVED_PARAM_KEYS.intersection(params))
	if conflicting:
		raise ValueError(f"params may not include reserved keys: {', '.join(conflicting)}")
	for key in REQUEST_SETTING_KEYS:
		if key not in params:
			continue
		if not isinstance(params[key], dict):
			raise ValueError(f"{key} must be a dict")
		reserved = RESERVED_HEADER_KEYS if key == "extra_headers" else RESERVED_PARAM_KEYS
		nested_keys = {str(value).lower() for value in params[key]}
		conflicting = sorted(reserved.intersection(nested_keys))
		if conflicting:
			raise ValueError(f"{key} may not include reserved keys: {', '.join(conflicting)}")


def _consume_stream(chunks: Any) -> Generator[str | ToolCallBegin, None, ChatResponse]:
	"""Yield text deltas (and a ToolCallBegin the moment each tool call's name is known) from a
	litellm stream; return the assembled ChatResponse at the end."""
	content_parts: list[str] = []
	tool_calls_acc: dict[int, dict[str, str]] = {}
	announced: set[int] = set()
	usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
	finish_reason: str | None = None

	for chunk in chunks:
		choices = getattr(chunk, "choices", None) or []
		if choices:
			choice = choices[0]
			delta = getattr(choice, "delta", None)
			if delta is not None:
				text = _attr(delta, "content")
				if text:
					content_parts.append(text)
					yield text
				for tc_delta in _attr(delta, "tool_calls") or []:
					index = _accumulate_tool_call(tool_calls_acc, tc_delta)
					slot = tool_calls_acc[index]
					if index not in announced and slot["id"] and slot["name"]:
						announced.add(index)
						yield ToolCallBegin(id=slot["id"], name=slot["name"])
			reason = _attr(choice, "finish_reason")
			if reason:
				finish_reason = reason

		usage_obj = getattr(chunk, "usage", None)
		if usage_obj is not None:
			usage = {
				"prompt_tokens": _attr(usage_obj, "prompt_tokens", 0) or 0,
				"completion_tokens": _attr(usage_obj, "completion_tokens", 0) or 0,
				"total_tokens": _attr(usage_obj, "total_tokens", 0) or 0,
			}

	return ChatResponse(
		content="".join(content_parts) or None,
		tool_calls=_finalize_tool_calls(tool_calls_acc),
		finish_reason=finish_reason,
		usage=usage,
	)


def _accumulate_tool_call(acc: dict[int, dict[str, str]], delta: Any) -> int:
	index = _attr(delta, "index", 0) or 0
	slot = acc.setdefault(index, {"id": "", "name": "", "arguments": ""})
	call_id = _attr(delta, "id")
	if call_id:
		slot["id"] = call_id
	function = _attr(delta, "function")
	if function is not None:
		name = _attr(function, "name")
		if name:
			slot["name"] = name
		args = _attr(function, "arguments")
		if args:
			slot["arguments"] += args
	return index


def _finalize_tool_calls(acc: dict[int, dict[str, str]]) -> list[ToolCall]:
	calls: list[ToolCall] = []
	for index in sorted(acc):
		slot = acc[index]
		if not slot["id"] and not slot["name"]:
			continue
		calls.append(_build_tool_call(slot["id"], slot["name"], slot["arguments"]))
	return calls


def _build_tool_call(call_id: str, name: str, raw_args: str) -> ToolCall:
	"""Parse a tool call's raw arguments. Malformed arguments become a `ToolCall.error` the agent
	feeds back to the model to retry, rather than an exception that fails the whole run."""
	raw_args = raw_args or ""
	if not raw_args:
		return ToolCall(id=call_id, name=name, arguments={})
	try:
		arguments = json.loads(raw_args)
	except (TypeError, ValueError):
		return ToolCall(
			id=call_id,
			name=name,
			arguments={},
			error=f"Invalid JSON in arguments for {name!r}: {raw_args[:200]}. Resend the call with valid JSON.",
		)
	if not isinstance(arguments, dict):
		return ToolCall(
			id=call_id,
			name=name,
			arguments={},
			error=f"Arguments for {name!r} must be a JSON object, got: {raw_args[:200]}.",
		)
	return ToolCall(id=call_id, name=name, arguments=arguments)


def _normalize(response: Any) -> ChatResponse:
	choice = response.choices[0]
	message = choice.message

	tool_calls = []
	for raw_call in getattr(message, "tool_calls", None) or []:
		function = getattr(raw_call, "function", None) or {}
		name = _attr(function, "name", "")
		raw_args = _attr(function, "arguments", "") or ""
		tool_calls.append(_build_tool_call(_attr(raw_call, "id", ""), name, raw_args))

	usage_obj = getattr(response, "usage", None)
	usage = {
		"prompt_tokens": _attr(usage_obj, "prompt_tokens", 0) or 0,
		"completion_tokens": _attr(usage_obj, "completion_tokens", 0) or 0,
		"total_tokens": _attr(usage_obj, "total_tokens", 0) or 0,
	}

	return ChatResponse(
		content=getattr(message, "content", None),
		tool_calls=tool_calls,
		finish_reason=getattr(choice, "finish_reason", None),
		usage=usage,
	)


def _attr(obj: Any, key: str, default: Any = None) -> Any:
	if obj is None:
		return default
	if isinstance(obj, dict):
		return obj.get(key, default)
	return getattr(obj, key, default)
