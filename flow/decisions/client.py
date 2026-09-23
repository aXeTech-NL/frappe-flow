"""Transport for the shared System One HTTP contract; no vendor SDK or ML runtime."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import ReadTimeoutError

from flow.decisions.schema import validate_questions, validate_response, validate_state

MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class DecisionError(RuntimeError):
	"""A safe-to-display failure. Provider bodies, state, URLs and credentials are not included."""

	def __init__(self, message: str, *, kind: str, status_code: int | None = None):
		super().__init__(message)
		self.kind = kind
		self.status_code = status_code


def validate_connection(endpoint_url: str, model_id: str | None, timeout: int) -> None:
	try:
		url = urlsplit(endpoint_url)
		valid = (
			isinstance(endpoint_url, str)
			and not any(c.isspace() or ord(c) < 32 for c in endpoint_url)
			and url.scheme in {"http", "https"}
			and bool(url.hostname)
			and url.username is None
			and url.password is None
			and not url.query
			and not url.fragment
		)
		url.port  # Validate the port as well.
	except (AttributeError, TypeError, ValueError):
		valid = False
	if not valid:
		raise ValueError(
			"Endpoint URL must be an absolute HTTP(S) URL without credentials, query, or fragment."
		)
	if model_id is not None and (not isinstance(model_id, str) or not model_id.strip()):
		raise ValueError("Model ID must be non-empty text when supplied.")
	if type(timeout) is not int or not 1 <= timeout <= 300:
		raise ValueError("Timeout must be an integer between 1 and 300 seconds.")


def validate_api_key(api_key: str | None) -> None:
	if api_key is not None and (
		not isinstance(api_key, str)
		or not api_key.isascii()
		or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in api_key)
	):
		raise ValueError("API key must contain only printable ASCII characters without whitespace.")


class SystemOneClient:
	def __init__(
		self,
		*,
		endpoint_url: str,
		api_key: str | None = None,
		model_id: str | None = None,
		timeout: int = 30,
	):
		validate_connection(endpoint_url, model_id, timeout)
		validate_api_key(api_key)
		self.endpoint_url = endpoint_url
		self._api_key = api_key
		self.model_id = model_id
		self.timeout = timeout

	def evaluate(self, *, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
		validate_state(state)
		validate_questions(questions)
		body = {"state": state, "questions": questions}
		if self.model_id is not None:
			body["model"] = self.model_id
		headers = {"Accept": "application/json", "Content-Type": "application/json"}
		if self._api_key:
			headers["Authorization"] = f"Bearer {self._api_key}"

		try:
			with requests.Session() as session:
				# Do not inherit .netrc credentials or environment proxies for an admin-configured endpoint.
				session.trust_env = False
				with session.post(
					self.endpoint_url,
					json=body,
					headers=headers,
					timeout=self.timeout,
					allow_redirects=False,
					stream=True,
				) as response:
					if response.status_code != 200:
						raise _http_error(response.status_code)
					data = bytearray()
					for chunk in response.iter_content(chunk_size=65536):
						data.extend(chunk)
						if len(data) > MAX_RESPONSE_BYTES:
							raise DecisionError("System One response is too large.", kind="invalid_response")
		except requests.Timeout:
			raise DecisionError("System One request timed out.", kind="timeout") from None
		except requests.ConnectionError as exc:
			# Requests wraps urllib3 timeouts during iter_content in ConnectionError.
			if exc.args and isinstance(exc.args[0], ReadTimeoutError):
				raise DecisionError("System One request timed out.", kind="timeout") from None
			raise DecisionError("Could not reach the System One provider.", kind="transport") from None
		except requests.RequestException:
			raise DecisionError("Could not reach the System One provider.", kind="transport") from None

		try:
			payload = json.loads(data)
			return validate_response(payload, questions)
		except (ValueError, TypeError, RecursionError, OverflowError):
			raise DecisionError(
				"System One provider returned an invalid response.", kind="invalid_response"
			) from None


def _http_error(status: int) -> DecisionError:
	if status in (401, 403):
		kind = "authentication"
	elif status == 429:
		kind = "rate_limit"
	elif status >= 500:
		kind = "unavailable"
	elif status in (400, 422):
		kind = "validation"
	else:
		kind = "http"
	return DecisionError(f"System One request failed (HTTP {status}).", kind=kind, status_code=status)
