"""Unit tests for the System One contract; no database or live provider required."""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import requests
from urllib3.exceptions import ReadTimeoutError

from flow.decisions import DecisionError, SystemOneClient, choice, evaluate, noul, score
from flow.decisions.schema import parse_questions, validate_questions, validate_response, validate_state


def _questions():
	return {
		"team": choice("Who should handle this?", {"billing": "Invoices", "technical": "Bugs"}),
		"urgency": score("How urgent is it?", ["Normal", "Urgent", "Critical"]),
		"refund": noul("Is a refund requested?"),
	}


def _response():
	return {
		"model": "my-model-v1",
		"answers": {
			"team": {
				"type": "choice",
				"choice": "billing",
				"probabilities": {"billing": 0.8, "technical": 0.2},
				"confidence": 0.7,
			},
			"urgency": {
				"type": "score",
				"score": 1.2,
				"legend": {"0": "Normal", "1": "Urgent", "2": "Critical"},
				"probabilities": {"0": 0.1, "1": 0.6, "2": 0.3},
				"confidence": 0.6,
			},
			"refund": {"type": "noul", "noul": 0.91},
		},
		"usage": {"input_tokens": 150, "output_tokens": 20},
	}


class TestDecisionSchema(TestCase):
	def test_helpers_match_wire_contract(self):
		self.assertEqual(noul("Urgent?"), {"type": "noul", "instructions": "Urgent?"})
		self.assertEqual(choice("Team?", {"a": None})["criteria"], {"a": None})
		self.assertEqual(score("Rate?", ["low", "high"])["criteria"], ["low", "high"])

	def test_structured_descriptions_and_instructions(self):
		validate_questions(
			{
				"match": choice({"question": "Which?", "context": ["abc"]}, {"one": {"meaning": "first"}}),
				"yes": noul(["Is this urgent?"], {"true": "Urgent", "false": "Not urgent"}),
			}
		)

	def test_parse_saved_questions(self):
		self.assertEqual(parse_questions(json.dumps(_questions())), _questions())

	def test_invalid_question_shapes(self):
		invalid = [
			{},
			[],
			None,
			{"": noul("Yes?")},
			{1: noul("Yes?")},
			{"q": []},
			{"q": {"type": "chat", "instructions": "Write"}},
			{"q": {"type": [], "instructions": "Write"}},
			{"q": {"type": "noul", "instructions": " "}},
			{"q": {"type": "noul", "instructions": "Yes?", "tools": []}},
			{"q": {"type": "choice", "instructions": "Which?", "criteria": {}}},
			{"q": {"type": "choice", "instructions": "Which?", "criteria": {"a": 2}}},
			{"q": {"type": "score", "instructions": "Rate?", "criteria": ["one"]}},
			{"q": {"type": "noul", "instructions": "Yes?", "criteria": {"yes": "yes"}}},
		]
		for value in invalid:
			with self.subTest(value=value), self.assertRaises(ValueError):
				validate_questions(value)

	def test_malformed_saved_json(self):
		for value in ("{bad", "[]", "null"):
			with self.subTest(value=value), self.assertRaises(ValueError):
				parse_questions(value)

	def test_state_accepts_json_but_not_python_objects(self):
		for value in ("hello", {}, [], {"nested": [1, True, None, 0.1]}):
			validate_state(value)
		for value in (None, 42, {"a": float("nan")}, {"a": float("inf")}, {1: "a"}, {"a": object()}):
			with self.subTest(value=value), self.assertRaises(ValueError):
				validate_state(value)

	def test_mixed_response_retains_native_values_and_metadata(self):
		payload = _response()
		payload["routing"] = {"selected": "multilingual"}
		self.assertIs(validate_response(payload, _questions()), payload)
		self.assertEqual(payload["answers"]["urgency"]["score"], 1.2)
		self.assertNotIn("confidence", payload["answers"]["refund"])

	def test_optional_usage(self):
		for usage in (None, {}, {"input_tokens": None, "output_tokens": 0}):
			payload = _response()
			payload["usage"] = usage
			validate_response(payload, _questions())
		payload.pop("usage")
		validate_response(payload, _questions())

	def test_answer_ids_must_match_exactly(self):
		for change in ("missing", "extra"):
			payload = _response()
			if change == "missing":
				del payload["answers"]["refund"]
			else:
				payload["answers"]["other"] = {"type": "noul", "noul": 0.2}
			with self.subTest(change=change), self.assertRaises(ValueError):
				validate_response(payload, _questions())

	def test_rejects_invalid_answers(self):
		changes = [
			("refund", "type", "choice"),
			("refund", "noul", True),
			("refund", "noul", -0.1),
			("refund", "noul", float("nan")),
			("refund", "noul", 1.1),
			("team", "choice", "other"),
			("team", "choice", "technical"),
			("team", "choice", []),
			("team", "confidence", float("inf")),
			("team", "confidence", "0.5"),
			("team", "probabilities", {"billing": 0.9, "technical": 0.9}),
			("team", "probabilities", {"billing": 1.0}),
			("urgency", "legend", {"0": "wrong", "1": "Urgent", "2": "Critical"}),
			("urgency", "score", 5),
			("urgency", "score", True),
			("urgency", "score", 0.5),
		]
		for key, field, value in changes:
			payload = _response()
			payload["answers"][key][field] = value
			with self.subTest(key=key, field=field, value=value), self.assertRaises(ValueError):
				validate_response(payload, _questions())

	def test_ties_and_rounded_probabilities(self):
		payload = _response()
		payload["answers"]["team"]["probabilities"] = {"billing": 0.5, "technical": 0.5}
		validate_response(payload, _questions())
		payload["answers"]["team"]["probabilities"] = {"billing": 0.7999, "technical": 0.2}
		validate_response(payload, _questions())

	def test_invalid_envelopes_and_usage(self):
		for value in ([], None, {}, {"model": 1, "answers": {}}):
			with self.subTest(value=value), self.assertRaises(ValueError):
				validate_response(value, _questions())
		for usage in ([], {"input_tokens": True}, {"output_tokens": -1}, {"input_tokens": "20"}):
			payload = _response()
			payload["usage"] = usage
			with self.subTest(usage=usage), self.assertRaises(ValueError):
				validate_response(payload, _questions())


class TestSystemOneClient(TestCase):
	def setUp(self):
		self.patcher = patch("flow.decisions.client.requests.Session")
		self.factory = self.patcher.start()
		self.addCleanup(self.patcher.stop)
		self.session = self.factory.return_value.__enter__.return_value
		self.response = self.session.post.return_value.__enter__.return_value
		self.response.status_code = 200
		self.response.iter_content.return_value = [json.dumps(_response()).encode()]
		self.client = SystemOneClient(
			endpoint_url="https://provider.example/custom/systemone",
			api_key="secret",
			model_id="configured-model",
		)

	def test_generic_endpoint_request_and_result(self):
		state = {"subject": "Refund please"}
		self.assertEqual(self.client.evaluate(state=state, questions=_questions()), _response())
		self.session.post.assert_called_once_with(
			"https://provider.example/custom/systemone",
			json={"model": "configured-model", "state": state, "questions": _questions()},
			headers={
				"Accept": "application/json",
				"Content-Type": "application/json",
				"Authorization": "Bearer secret",
			},
			timeout=30,
			allow_redirects=False,
			stream=True,
		)
		self.assertFalse(self.session.trust_env)
		self.session.post.return_value.__exit__.assert_called_once()

	def test_self_hosted_without_key_or_model(self):
		client = SystemOneClient(endpoint_url="http://localhost:8000/v1/systemone", timeout=5)
		client.evaluate(state="hello", questions=_questions())
		kwargs = self.session.post.call_args.kwargs
		self.assertNotIn("Authorization", kwargs["headers"])
		self.assertNotIn("model", kwargs["json"])
		self.assertEqual(kwargs["timeout"], 5)

	def test_validation_prevents_network_calls(self):
		with self.assertRaises(ValueError):
			self.client.evaluate(state={"bad": float("nan")}, questions=_questions())
		with self.assertRaises(ValueError):
			self.client.evaluate(state="hello", questions={})
		self.factory.assert_not_called()

	def test_invalid_connection_settings(self):
		for url in (
			"relative",
			"file:///tmp/a",
			"ftp://example.com",
			"https://a:bad/v1",
			"https://x:y@a/v1",
			"https://a/v1?k=secret",
			"https://a/v1#fragment",
			"https://a/\npath",
			"http://[bad",
		):
			with self.subTest(url=url), self.assertRaises(ValueError):
				SystemOneClient(endpoint_url=url)
		for timeout in (0, -1, 301, True, 1.5):
			with self.subTest(timeout=timeout), self.assertRaises(ValueError):
				SystemOneClient(endpoint_url="https://a/v1", timeout=timeout)
		with self.assertRaises(ValueError):
			SystemOneClient(endpoint_url="https://a/v1", api_key="secret\nheader")

	def test_http_errors_are_sanitized_without_retry(self):
		for status, kind in (
			(401, "authentication"),
			(403, "authentication"),
			(422, "validation"),
			(429, "rate_limit"),
			(529, "unavailable"),
			(500, "unavailable"),
			(302, "http"),
		):
			self.session.post.reset_mock()
			self.response.status_code = status
			self.response.text = "echoed-secret-and-private-state"
			with self.subTest(status=status), self.assertRaises(DecisionError) as caught:
				self.client.evaluate(state="private", questions=_questions())
			self.assertEqual(caught.exception.kind, kind)
			self.assertEqual(caught.exception.status_code, status)
			self.assertNotIn("secret", str(caught.exception))
			self.session.post.assert_called_once()

	def test_transport_failures_are_sanitized(self):
		for exc, kind in (
			(requests.Timeout("secret"), "timeout"),
			(requests.ConnectionError("secret"), "transport"),
		):
			self.session.post.side_effect = exc
			with self.subTest(kind=kind), self.assertRaises(DecisionError) as caught:
				self.client.evaluate(state="private", questions=_questions())
			self.assertEqual(caught.exception.kind, kind)
			self.assertNotIn("secret", str(caught.exception))
			self.assertTrue(caught.exception.__suppress_context__)

	def test_malformed_and_incomplete_json_rejected(self):
		for data in (
			b"not JSON secret",
			b"[]",
			b"{}",
			b"\xff",
			json.dumps({"model": "x", "answers": {}}).encode(),
		):
			self.response.iter_content.return_value = [data]
			with self.subTest(data=data), self.assertRaises(DecisionError) as caught:
				self.client.evaluate(state="private", questions=_questions())
			self.assertEqual(caught.exception.kind, "invalid_response")
			self.assertNotIn("secret", str(caught.exception))

	def test_response_size_is_bounded(self):
		with patch("flow.decisions.client.MAX_RESPONSE_BYTES", 8), self.assertRaises(DecisionError):
			self.client.evaluate(state="hello", questions=_questions())
		self.session.post.return_value.__exit__.assert_called_once()

	def test_non_ascii_and_control_character_keys_fail_before_transport(self):
		for token in ("secret\u20ac", "secret\u00e9", "secret\x7f", "secret key"):
			with self.subTest(token=token), self.assertRaises(ValueError) as caught:
				SystemOneClient(endpoint_url="https://provider.example/v1/systemone", api_key=token)
			self.assertNotIn("secret", str(caught.exception))
		self.factory.assert_not_called()

	def test_real_requests_stream_read_timeout_is_classified(self):
		response = requests.Response()
		response.status_code = 200
		response.raw = MagicMock()
		response.raw.stream.side_effect = ReadTimeoutError(None, "/private-url", "private-state")
		self.session.post.return_value = response
		with self.assertRaises(DecisionError) as caught:
			self.client.evaluate(state="private", questions=_questions())
		self.assertEqual(caught.exception.kind, "timeout")
		self.assertNotIn("private", str(caught.exception))
		self.assertTrue(caught.exception.__suppress_context__)
		response.raw.close.assert_called_once()

	def test_stream_read_failure_closes_response(self):
		self.response.iter_content.side_effect = requests.ConnectionError("private-url")
		with self.assertRaises(DecisionError):
			self.client.evaluate(state="private", questions=_questions())
		self.session.post.return_value.__exit__.assert_called_once()


class TestSystemOneHTTP(TestCase):
	"""Exercise real HTTP serialization and redirect handling against a local fake provider."""

	@classmethod
	def setUpClass(cls):
		class Handler(BaseHTTPRequestHandler):
			def do_POST(self):
				body = self.rfile.read(int(self.headers["Content-Length"]))
				cls.received.append((self.path, dict(self.headers), json.loads(body)))
				self.send_response(cls.status)
				self.send_header("Content-Type", "application/json")
				self.send_header("Content-Length", str(len(cls.payload)))
				if cls.status == 302:
					self.send_header("Location", "/redirect-target")
				self.end_headers()
				self.wfile.write(cls.payload)

			def log_message(self, *args):
				pass

		cls.server = HTTPServer(("127.0.0.1", 0), Handler)
		cls.thread = Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
		cls.thread.start()
		cls.url = f"http://127.0.0.1:{cls.server.server_port}/v1/systemone"

	@classmethod
	def tearDownClass(cls):
		cls.server.shutdown()
		cls.thread.join()
		cls.server.server_close()

	def setUp(self):
		type(self).received = []
		type(self).status = 200
		type(self).payload = json.dumps(_response()).encode()

	def test_real_http_roundtrip_with_utf8_and_bearer_key(self):
		client = SystemOneClient(endpoint_url=self.url, api_key="local-test-key", model_id="any-model")
		state = {"ticket": "Factuur dubbel betaald — graag terugbetalen"}
		self.assertEqual(client.evaluate(state=state, questions=_questions()), _response())
		path, headers, payload = self.received[0]
		self.assertEqual(path, "/v1/systemone")
		self.assertEqual(headers["Authorization"], "Bearer local-test-key")
		self.assertEqual(payload, {"state": state, "model": "any-model", "questions": _questions()})

	def test_no_auth_or_model_added_for_server_defaults(self):
		SystemOneClient(endpoint_url=self.url).evaluate(state="hello", questions=_questions())
		_, headers, payload = self.received[0]
		self.assertNotIn("Authorization", headers)
		self.assertNotIn("model", payload)

	def test_redirect_is_not_followed(self):
		type(self).status = 302
		with self.assertRaises(DecisionError) as caught:
			SystemOneClient(endpoint_url=self.url, api_key="secret").evaluate(
				state="hello", questions=_questions()
			)
		self.assertEqual(caught.exception.status_code, 302)
		self.assertEqual(len(self.received), 1)

	def test_malformed_provider_response_fails_closed(self):
		type(self).payload = b'{"model":"local","answers":{}}'
		with self.assertRaises(DecisionError) as caught:
			SystemOneClient(endpoint_url=self.url).evaluate(state="hello", questions=_questions())
		self.assertEqual(caught.exception.kind, "invalid_response")


class TestDecisionService(TestCase):
	def setUp(self):
		self.frappe = MagicMock()
		self.frappe.session = SimpleNamespace(user="test@example.com")
		self.frappe.PermissionError = PermissionError
		self.connection = MagicMock(
			enabled=True,
			endpoint_url="https://provider.example/v1/systemone",
			model_id="provider-model",
			timeout=30,
		)
		self.connection.get_password.return_value = "server-secret"
		self.decision = MagicMock(
			enabled=True, provider="custom-provider", model_id="", questions=json.dumps(_questions())
		)
		self.frappe.get_doc.side_effect = (
			lambda doctype, name: self.decision if doctype == "Flow Decision" else self.connection
		)
		patcher = patch.dict("sys.modules", {"frappe": self.frappe})
		patcher.start()
		self.addCleanup(patcher.stop)
		patcher = patch("flow.decisions.service.SystemOneClient")
		self.client = patcher.start()
		self.addCleanup(patcher.stop)
		self.client.return_value.evaluate.return_value = _response()

	def test_saved_definition_delegates_only_its_configured_provider(self):
		self.assertEqual(evaluate("triage", state="ticket"), _response())
		self.decision.check_permission.assert_called_once_with("read")
		self.connection.check_permission.assert_not_called()
		self.client.assert_called_once_with(
			endpoint_url=self.connection.endpoint_url,
			api_key="server-secret",
			model_id="provider-model",
			timeout=30,
		)
		self.client.return_value.evaluate.assert_called_once_with(state="ticket", questions=_questions())

	def test_inline_requires_provider_read_permission(self):
		evaluate(provider="custom-provider", state="ticket", questions=_questions())
		self.connection.check_permission.assert_called_once_with("read")

	def test_saved_model_override(self):
		self.decision.model_id = "decision-model"
		evaluate("triage", state="ticket")
		self.assertEqual(self.client.call_args.kwargs["model_id"], "decision-model")

	def test_inline_model_override(self):
		evaluate(provider="custom-provider", state="ticket", questions=_questions(), model_id="inline-model")
		self.assertEqual(self.client.call_args.kwargs["model_id"], "inline-model")

	def test_denied_decision_cannot_load_provider_or_credentials(self):
		self.decision.check_permission.side_effect = PermissionError
		with self.assertRaises(PermissionError):
			evaluate("triage", state="ticket")
		self.frappe.get_doc.assert_called_once_with("Flow Decision", "triage")
		self.connection.get_password.assert_not_called()
		self.client.assert_not_called()

	def test_denied_inline_provider_cannot_load_credentials(self):
		self.connection.check_permission.side_effect = PermissionError
		with self.assertRaises(PermissionError):
			evaluate(provider="custom-provider", state="ticket", questions=_questions())
		self.connection.get_password.assert_not_called()
		self.client.assert_not_called()

	def test_guest_rejected_before_any_lookup(self):
		self.frappe.session.user = "Guest"
		with self.assertRaises(PermissionError):
			evaluate("triage", state="ticket")
		self.frappe.get_doc.assert_not_called()

	def test_disabled_decision_or_provider_never_calls_network(self):
		for doc in (self.decision, self.connection):
			doc.enabled = False
			with self.assertRaises(DecisionError) as caught:
				evaluate("triage", state="ticket")
			self.assertEqual(caught.exception.kind, "configuration")
			doc.enabled = True
		self.client.assert_not_called()
		self.connection.get_password.assert_not_called()

	def test_no_overrides_of_saved_definitions(self):
		for kwargs in ({"provider": "evil"}, {"questions": _questions()}, {"model_id": "override"}):
			with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
				evaluate("triage", state="ticket", **kwargs)
		self.frappe.get_doc.assert_not_called()

	def test_explicit_inputs_required(self):
		for kwargs in ({}, {"provider": ""}, {"decision": ""}, {"provider": "custom-provider"}):
			with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
				evaluate(state="ticket", **kwargs)
		self.client.assert_not_called()
