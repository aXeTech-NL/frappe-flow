# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests import UnitTestCase

from flow.lib.model import (
	API_STYLE_CHAT_COMPLETIONS,
	API_STYLE_PROVIDER_DEFAULT,
	API_STYLE_RESPONSES,
	ChatResponse,
	Model,
	ToolCall,
	ToolCallBegin,
	route_model_id,
)


def _fake_response(content=None, tool_calls=None, finish_reason="stop", usage=None):
	usage = usage or {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
	message = SimpleNamespace(content=content, tool_calls=tool_calls)
	choice = SimpleNamespace(message=message, finish_reason=finish_reason)
	return SimpleNamespace(choices=[choice], usage=SimpleNamespace(**usage))


def _stream_chunk(content=None, tool_calls=None, finish_reason=None, usage=None):
	delta = SimpleNamespace(content=content, tool_calls=tool_calls)
	choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
	usage_obj = SimpleNamespace(**usage) if usage else None
	return SimpleNamespace(choices=[choice], usage=usage_obj)


def _tc_delta(index=0, id=None, name=None, arguments=None):
	function = SimpleNamespace(name=name, arguments=arguments)
	return SimpleNamespace(index=index, id=id, function=function)


def _drain(stream):
	"""Iterate a generator, returning (yielded_values, generator_return_value)."""
	yielded = []
	while True:
		try:
			yielded.append(next(stream))
		except StopIteration as e:
			return yielded, e.value


class TestModel(UnitTestCase):
	def test_init_requires_model_id(self):
		with self.assertRaises(ValueError):
			Model(model_id="", api_key="x")

	def test_init_allows_empty_api_key_for_local_providers(self):
		m = Model(model_id="ollama/llama3.1", base_url="http://localhost:11434")
		self.assertIsNone(m._api_key)

	def test_init_rejects_name_with_kwargs(self):
		with self.assertRaises(ValueError):
			Model("Some Model", model_id="openai/gpt-4.1")

	@patch("frappe.get_doc")
	def test_init_from_doc_name(self, mock_get_doc):
		mock_doc = SimpleNamespace(
			enabled=1,
			model_id="anthropic/claude-sonnet-4-6",
			base_url=None,
			params='{"temperature": 0.3}',
			get_password=lambda field, raise_exception=False: "sk-stored",
		)
		mock_get_doc.return_value = mock_doc

		m = Model("Claude Sonnet 4.6")

		mock_get_doc.assert_called_once_with("Flow Model", "Claude Sonnet 4.6")
		self.assertEqual(m.model_id, "anthropic/claude-sonnet-4-6")
		self.assertEqual(m._api_key, "sk-stored")
		self.assertEqual(m.params, {"temperature": 0.3})
		self.assertEqual(m.api_style, "Auto")

	@patch("flow.lib.model.resolve_provider_credentials")
	@patch("frappe.get_doc")
	def test_linked_model_keeps_remote_id_and_routes_with_connector(self, mock_get_doc, credentials):
		mock_get_doc.return_value = SimpleNamespace(
			enabled=1,
			model_id="hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q5_K_XL",
			provider="OpenWebUI",
			api_style="Provider Default",
			base_url=None,
			params=None,
			get_password=lambda field, raise_exception=False: None,
		)
		credentials.return_value = {
			"connector": "openai",
			"api_style": "Chat Completions",
			"base_url": "https://openwebui.example.com/v1",
		}

		model = Model("OpenWebUI Qwen")
		kwargs = model.completion_kwargs([{"role": "user", "content": "hi"}])

		self.assertEqual(model.model_id, "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q5_K_XL")
		self.assertEqual(kwargs["model"], "openai/hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q5_K_XL")
		self.assertEqual(kwargs["api_base"], "https://openwebui.example.com/v1")

	@patch("frappe.get_doc")
	def test_init_rejects_disabled_doc(self, mock_get_doc):
		mock_get_doc.return_value = SimpleNamespace(
			enabled=0,
			model_id="anthropic/claude-sonnet-4-6",
			base_url=None,
			params=None,
			get_password=lambda field, raise_exception=False: "sk-stored",
		)
		with self.assertRaises(ValueError):
			Model("Disabled Model")

	@patch("litellm.completion")
	def test_chat_accepts_string_message(self, mock_completion):
		mock_completion.return_value = _fake_response(content="hi there")

		m = Model(model_id="openai/gpt-4.1", api_key="sk-test")
		m.chat("hi")

		messages = mock_completion.call_args.kwargs["messages"]
		self.assertEqual(messages, [{"role": "user", "content": "hi"}])

	@patch("litellm.completion")
	def test_chat_normalizes_text_response(self, mock_completion):
		mock_completion.return_value = _fake_response(content="hi there")

		m = Model(model_id="openai/gpt-4.1", api_key="sk-test")
		resp = m.chat([{"role": "user", "content": "hi"}])

		self.assertIsInstance(resp, ChatResponse)
		self.assertEqual(resp.content, "hi there")
		self.assertEqual(resp.tool_calls, [])
		self.assertEqual(resp.finish_reason, "stop")
		self.assertEqual(resp.usage["total_tokens"], 8)

	@patch("litellm.completion")
	def test_chat_parses_tool_calls(self, mock_completion):
		raw_call = SimpleNamespace(
			id="call_abc",
			function=SimpleNamespace(name="get_weather", arguments='{"city": "Mumbai"}'),
		)
		mock_completion.return_value = _fake_response(
			content=None, tool_calls=[raw_call], finish_reason="tool_calls"
		)

		m = Model(model_id="openai/gpt-4.1", api_key="sk-test")
		resp = m.chat([{"role": "user", "content": "weather?"}])

		self.assertEqual(len(resp.tool_calls), 1)
		call = resp.tool_calls[0]
		self.assertIsInstance(call, ToolCall)
		self.assertEqual(call.id, "call_abc")
		self.assertEqual(call.name, "get_weather")
		self.assertEqual(call.arguments, {"city": "Mumbai"})
		self.assertEqual(resp.finish_reason, "tool_calls")
		self.assertEqual(mock_completion.call_args.kwargs["model"], "openai/responses/gpt-4.1")

	@patch("litellm.completion")
	def test_chat_returns_error_for_invalid_tool_arguments(self, mock_completion):
		raw_call = SimpleNamespace(
			id="call_bad",
			function=SimpleNamespace(name="broken", arguments="{not json"),
		)
		mock_completion.return_value = _fake_response(tool_calls=[raw_call])

		m = Model(model_id="openai/gpt-4.1", api_key="sk-test")
		resp = m.chat([{"role": "user", "content": "hi"}])

		call = resp.tool_calls[0]
		self.assertEqual(call.arguments, {})
		self.assertIn("Invalid JSON", call.error)

	@patch("litellm.completion")
	def test_chat_stream_yields_text_deltas_and_assembles_response(self, mock_completion):
		mock_completion.return_value = iter(
			[
				_stream_chunk(content="hello "),
				_stream_chunk(content="world"),
				_stream_chunk(
					finish_reason="stop",
					usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
				),
			]
		)

		m = Model(model_id="openai/gpt-4.1", api_key="sk-test")
		yielded, response = _drain(m.chat([{"role": "user", "content": "hi"}], stream=True))

		self.assertEqual(yielded, ["hello ", "world"])
		self.assertIsInstance(response, ChatResponse)
		self.assertEqual(response.content, "hello world")
		self.assertEqual(response.finish_reason, "stop")
		self.assertEqual(response.usage["total_tokens"], 7)
		self.assertEqual(response.tool_calls, [])
		self.assertEqual(mock_completion.call_args.kwargs["model"], "openai/responses/gpt-4.1")

	@patch("litellm.completion")
	def test_chat_stream_assembles_tool_calls_across_chunks(self, mock_completion):
		mock_completion.return_value = iter(
			[
				_stream_chunk(tool_calls=[_tc_delta(id="call_a", name="add", arguments='{"a":1')]),
				_stream_chunk(tool_calls=[_tc_delta(arguments=',"b":2}')]),
				_stream_chunk(
					finish_reason="tool_calls",
					usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
				),
			]
		)

		m = Model(model_id="openai/gpt-4.1", api_key="sk-test")
		yielded, response = _drain(m.chat([{"role": "user", "content": "hi"}], stream=True))

		# The call is announced mid-stream the moment its id+name are known.
		self.assertEqual(yielded, [ToolCallBegin(id="call_a", name="add")])
		self.assertEqual(len(response.tool_calls), 1)
		call = response.tool_calls[0]
		self.assertIsInstance(call, ToolCall)
		self.assertEqual(call.id, "call_a")
		self.assertEqual(call.name, "add")
		self.assertEqual(call.arguments, {"a": 1, "b": 2})
		self.assertEqual(response.finish_reason, "tool_calls")

	@patch("litellm.completion")
	def test_chat_stream_passes_stream_options_for_usage(self, mock_completion):
		mock_completion.return_value = iter([_stream_chunk(content="hi", finish_reason="stop")])

		m = Model(model_id="openai/gpt-4.1", api_key="sk-test")
		_drain(m.chat([{"role": "user", "content": "hi"}], stream=True))

		kwargs = mock_completion.call_args.kwargs
		self.assertTrue(kwargs["stream"])
		self.assertEqual(kwargs["stream_options"], {"include_usage": True})

	@patch("litellm.completion")
	def test_chat_stream_returns_error_for_invalid_tool_arguments(self, mock_completion):
		mock_completion.return_value = iter(
			[
				_stream_chunk(tool_calls=[_tc_delta(id="c1", name="boom", arguments="{not json")]),
				_stream_chunk(finish_reason="tool_calls"),
			]
		)

		m = Model(model_id="openai/gpt-4.1", api_key="sk-test")
		_yielded, response = _drain(m.chat([{"role": "user", "content": "hi"}], stream=True))

		call = response.tool_calls[0]
		self.assertEqual(call.arguments, {})
		self.assertIn("Invalid JSON", call.error)

	@patch("litellm.completion")
	def test_chat_forwards_params_tools_and_base_url(self, mock_completion):
		mock_completion.return_value = _fake_response(content="ok")

		tools = [{"type": "function", "function": {"name": "ping", "parameters": {}}}]
		m = Model(
			model_id="anthropic/claude-sonnet-4-6",
			api_key="sk-test",
			base_url="http://localhost:11434",
			params={"temperature": 0.2, "max_tokens": 100},
		)
		m.chat([{"role": "user", "content": "hi"}], tools=tools)

		kwargs = mock_completion.call_args.kwargs
		self.assertEqual(kwargs["model"], "anthropic/claude-sonnet-4-6")
		self.assertEqual(kwargs["api_key"], "sk-test")
		self.assertEqual(kwargs["api_base"], "http://localhost:11434")
		self.assertEqual(kwargs["temperature"], 0.2)
		self.assertEqual(kwargs["max_tokens"], 100)
		self.assertEqual(kwargs["tools"], tools)
		self.assertEqual(kwargs["timeout"], 60)

	def test_route_model_id_auto_routes_only_direct_openai(self):
		self.assertEqual(route_model_id("openai/gpt-4.1"), "openai/responses/gpt-4.1")
		self.assertEqual(
			route_model_id("openai/gpt-4.1", base_url="https://proxy.example.com/v1"),
			"openai/gpt-4.1",
		)
		self.assertEqual(route_model_id("anthropic/claude-sonnet-4-6"), "anthropic/claude-sonnet-4-6")

	def test_route_model_id_honors_explicit_openai_overrides(self):
		self.assertEqual(
			route_model_id(
				"openai/gpt-4.1",
				API_STYLE_RESPONSES,
				"https://proxy.example.com/v1",
			),
			"openai/responses/gpt-4.1",
		)
		self.assertEqual(
			route_model_id("openai/responses/gpt-4.1", API_STYLE_CHAT_COMPLETIONS),
			"openai/gpt-4.1",
		)

	def test_route_model_id_bridges_custom_openai_compatible_styles(self):
		self.assertEqual(
			route_model_id("openai_like/custom-model", API_STYLE_RESPONSES),
			"openai/responses/custom-model",
		)
		self.assertEqual(
			route_model_id("openai_like/responses/custom-model", API_STYLE_CHAT_COMPLETIONS),
			"openai/custom-model",
		)
		self.assertEqual(
			route_model_id(
				"openai_like/hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q5_K_XL",
				base_url="https://openwebui.example.com/v1",
			),
			"openai/hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q5_K_XL",
		)

	@patch("flow.lib.model.resolve_provider_credentials")
	def test_provider_default_selects_responses_url_and_request_settings(self, credentials):
		credentials.return_value = {
			"api_key": "sk-provider",
			"api_style": "Responses",
			"base_url": "https://generic.example.com/v1",
			"chat_base_url": "https://chat.example.com/v1",
			"responses_base_url": "https://responses.example.com/v1",
			"extra_params": {"temperature": 0.1, "seed": 1},
			"extra_headers": {"X-Provider": "yes", "X-Shared": "provider"},
			"extra_query": {"api-version": "v1"},
			"extra_body": {"metadata": {"source": "provider"}},
		}
		model = Model(
			model_id="openai/gpt-4.1",
			api_style=API_STYLE_PROVIDER_DEFAULT,
			params={
				"temperature": 0.8,
				"extra_headers": {"X-Shared": "model", "X-Model": "yes"},
			},
		)
		kwargs = model.completion_kwargs([{"role": "user", "content": "hi"}])
		self.assertEqual(kwargs["model"], "openai/responses/gpt-4.1")
		self.assertEqual(kwargs["api_base"], "https://responses.example.com/v1")
		self.assertEqual(kwargs["api_key"], "sk-provider")
		self.assertEqual(kwargs["temperature"], 0.8)
		self.assertEqual(kwargs["seed"], 1)
		self.assertEqual(
			kwargs["extra_headers"],
			{"X-Provider": "yes", "X-Shared": "model", "X-Model": "yes"},
		)
		self.assertEqual(kwargs["extra_query"], {"api-version": "v1"})

	@patch("flow.lib.model.resolve_provider_credentials")
	def test_base_url_precedence_and_proxy_auto_routing(self, credentials):
		credentials.return_value = {
			"api_style": "Auto",
			"base_url": "https://proxy.example.com/v1",
			"chat_base_url": "https://chat-proxy.example.com/v1",
			"responses_base_url": "https://responses-proxy.example.com/v1",
		}
		provider_model = Model(model_id="openai/gpt-4.1", api_style=API_STYLE_PROVIDER_DEFAULT)
		provider_kwargs = provider_model.completion_kwargs([{"role": "user", "content": "hi"}])
		self.assertEqual(provider_kwargs["model"], "openai/gpt-4.1")
		self.assertEqual(provider_kwargs["api_base"], "https://chat-proxy.example.com/v1")

		model_override = Model(
			model_id="openai/gpt-4.1",
			base_url="https://model.example.com/v1",
			api_style=API_STYLE_RESPONSES,
		)
		override_kwargs = model_override.completion_kwargs([{"role": "user", "content": "hi"}])
		self.assertEqual(override_kwargs["model"], "openai/responses/gpt-4.1")
		self.assertEqual(override_kwargs["api_base"], "https://model.example.com/v1")

	@patch("flow.lib.model.resolve_provider_credentials")
	def test_official_openai_auto_uses_responses_operation_override(self, credentials):
		credentials.return_value = {
			"api_style": "Auto",
			"base_url": None,
			"responses_base_url": "https://api.openai.example.com/v1",
		}
		model = Model(model_id="openai/gpt-4.1", api_style=API_STYLE_PROVIDER_DEFAULT)
		kwargs = model.completion_kwargs([{"role": "user", "content": "hi"}])
		self.assertEqual(kwargs["model"], "openai/responses/gpt-4.1")
		self.assertEqual(kwargs["api_base"], "https://api.openai.example.com/v1")

	def test_responses_bridge_sends_and_parses_tool_calls(self):
		seen = {}

		class Handler(BaseHTTPRequestHandler):
			def log_message(self, *_args):
				pass

			def do_POST(self):
				length = int(self.headers.get("content-length", "0"))
				seen["path"] = self.path
				seen["body"] = json.loads(self.rfile.read(length))
				payload = {
					"id": "resp_test",
					"object": "response",
					"created_at": 1,
					"status": "completed",
					"error": None,
					"incomplete_details": None,
					"instructions": None,
					"max_output_tokens": None,
					"model": "gpt-4.1",
					"output": [
						{
							"type": "function_call",
							"id": "fc_test",
							"call_id": "call_ping",
							"name": "ping",
							"arguments": json.dumps({"value": 1}),
							"status": "completed",
						}
					],
					"parallel_tool_calls": True,
					"previous_response_id": None,
					"reasoning": {"effort": None, "summary": None},
					"store": False,
					"temperature": 1.0,
					"text": {"format": {"type": "text"}},
					"tool_choice": "auto",
					"tools": [],
					"top_p": 1.0,
					"truncation": "disabled",
					"usage": {
						"input_tokens": 10,
						"input_tokens_details": {"cached_tokens": 0},
						"output_tokens": 5,
						"output_tokens_details": {"reasoning_tokens": 0},
						"total_tokens": 15,
					},
					"metadata": {},
				}
				raw = json.dumps(payload).encode()
				self.send_response(200)
				self.send_header("content-type", "application/json")
				self.send_header("content-length", str(len(raw)))
				self.end_headers()
				self.wfile.write(raw)

		server = HTTPServer(("127.0.0.1", 0), Handler)
		thread = threading.Thread(target=server.serve_forever, daemon=True)
		thread.start()
		try:
			model = Model(
				model_id="openai/gpt-4.1",
				api_key="sk-test",
				base_url=f"http://127.0.0.1:{server.server_port}/v1",
				api_style=API_STYLE_RESPONSES,
			)
			response = model.chat(
				[{"role": "user", "content": "call ping"}],
				tools=[
					{
						"type": "function",
						"function": {
							"name": "ping",
							"description": "Ping",
							"parameters": {
								"type": "object",
								"properties": {"value": {"type": "integer"}},
								"required": ["value"],
							},
						},
					}
				],
			)
		finally:
			server.shutdown()
			thread.join()

		self.assertEqual(seen["path"], "/v1/responses")
		self.assertEqual(seen["body"]["tools"][0]["name"], "ping")
		self.assertEqual(response.tool_calls[0].id, "call_ping")
		self.assertEqual(response.tool_calls[0].name, "ping")
		self.assertEqual(response.tool_calls[0].arguments, {"value": 1})

	@patch("litellm.completion")
	def test_chat_preserves_persisted_tool_result_messages_when_routing(self, mock_completion):
		mock_completion.return_value = _fake_response(content="done")
		messages = [
			{
				"role": "assistant",
				"content": None,
				"tool_calls": [
					{
						"id": "call_abc",
						"type": "function",
						"function": {"name": "get_weather", "arguments": '{"city":"Mumbai"}'},
					}
				],
			},
			{"role": "tool", "tool_call_id": "call_abc", "content": "sunny"},
		]

		Model(model_id="openai/gpt-4.1", api_key="sk-test").chat(messages)

		kwargs = mock_completion.call_args.kwargs
		self.assertEqual(kwargs["model"], "openai/responses/gpt-4.1")
		self.assertEqual(kwargs["messages"], messages)
