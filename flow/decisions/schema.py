"""JSON-native System One questions and response validation, independent of Frappe."""

from __future__ import annotations

import json
import math
from typing import Any

QUESTION_TYPES = frozenset({"choice", "score", "noul"})


def choice(instructions: Any, criteria: dict[str, Any]) -> dict[str, Any]:
	"""Choose one of the supplied option keys; descriptions may be null."""
	return _question("choice", instructions, criteria)


def score(instructions: Any, criteria: list[Any]) -> dict[str, Any]:
	"""Rate against ordered levels. Scores are zero-based, not necessarily 0-1."""
	return _question("score", instructions, criteria)


def noul(instructions: Any, criteria: dict[str, Any] | None = None) -> dict[str, Any]:
	"""Ask for P(yes), not a boolean or a separate confidence score."""
	return _question("noul", instructions, criteria)


def _question(kind: str, instructions: Any, criteria: Any) -> dict[str, Any]:
	question = {"type": kind, "instructions": instructions}
	if criteria is not None:
		question["criteria"] = criteria
	validate_questions({"question": question})
	return question


def _json_value(value: Any) -> None:
	if value is None or isinstance(value, str | bool | int):
		return
	if isinstance(value, float) and math.isfinite(value):
		return
	if isinstance(value, list):
		for item in value:
			_json_value(item)
		return
	if isinstance(value, dict) and all(isinstance(key, str) for key in value):
		for item in value.values():
			_json_value(item)
		return
	raise ValueError("Use JSON values with string object keys and finite numbers.")


def _content(value: Any) -> bool:
	return (
		isinstance(value, str | dict | list)
		and bool(value)
		and (not isinstance(value, str) or bool(value.strip()))
	)


def validate_questions(questions: Any) -> None:
	if not isinstance(questions, dict) or not questions:
		raise ValueError("Questions must be a non-empty object keyed by question ID.")
	_json_value(questions)
	for key, question in questions.items():
		if not key.strip() or not isinstance(question, dict):
			raise ValueError("Each question needs a non-empty ID and an object definition.")
		kind = question.get("type")
		if not isinstance(kind, str) or kind not in QUESTION_TYPES:
			raise ValueError("Question type must be choice, score, or noul.")
		if question.keys() - {"type", "instructions", "criteria"}:
			raise ValueError("Question fields must be type, instructions, and criteria only.")
		if not _content(question.get("instructions")):
			raise ValueError("Question instructions must be non-empty text, an object, or an array.")
		criteria = question.get("criteria")
		if kind == "choice":
			if not isinstance(criteria, dict) or not criteria or any(not k.strip() for k in criteria):
				raise ValueError("Choice criteria must be a non-empty object of option descriptions.")
			if any(v is not None and not _content(v) for v in criteria.values()):
				raise ValueError("Choice descriptions must be text, objects, arrays, or null.")
		elif kind == "score":
			if not isinstance(criteria, list) or len(criteria) < 2 or not all(map(_content, criteria)):
				raise ValueError("Score criteria must contain at least two ordered level descriptions.")
		elif criteria is not None:
			if not isinstance(criteria, dict) or criteria.keys() - {"true", "false"}:
				raise ValueError("Noul criteria may only describe true and false.")
			if not all(map(_content, criteria.values())):
				raise ValueError("Noul descriptions must be text, objects, or arrays.")


def parse_questions(value: str | dict[str, Any]) -> dict[str, Any]:
	"""Read saved JSON without accepting non-standard NaN or Infinity values."""
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError:
			raise ValueError("Questions must be valid JSON.") from None
	validate_questions(value)
	return value


def validate_state(state: Any) -> None:
	if not isinstance(state, str | dict | list):
		raise ValueError("State must be text, a JSON object, or an array.")
	_json_value(state)


def _number(value: Any) -> bool:
	return type(value) in (int, float) and math.isfinite(value)


def _probability(value: Any) -> bool:
	return _number(value) and 0 <= value <= 1


def validate_response(payload: Any, questions: dict[str, Any]) -> dict[str, Any]:
	"""Reject incomplete or inconsistent decisions rather than inventing fallback answers."""
	_json_value(payload)
	if not isinstance(payload, dict) or not isinstance(payload.get("model"), str) or not payload["model"]:
		raise ValueError("Missing response model.")
	answers = payload.get("answers")
	if not isinstance(answers, dict) or answers.keys() != questions.keys():
		raise ValueError("Response question IDs do not match the request.")
	for key, question in questions.items():
		answer = answers[key]
		kind = question["type"]
		if not isinstance(answer, dict) or answer.get("type") != kind:
			raise ValueError("Response answer type does not match the question.")
		if kind == "noul":
			if not _probability(answer.get("noul")):
				raise ValueError("Noul must be a probability between zero and one.")
			continue
		if not _probability(answer.get("confidence")):
			raise ValueError("Confidence must be between zero and one.")
		criteria = question["criteria"]
		options = set(criteria) if kind == "choice" else {str(i) for i in range(len(criteria))}
		probabilities = answer.get("probabilities")
		if (
			not isinstance(probabilities, dict)
			or probabilities.keys() != options
			or not all(map(_probability, probabilities.values()))
			or not math.isclose(sum(probabilities.values()), 1, abs_tol=0.001)
		):
			raise ValueError("Response probabilities must cover every option and sum to one.")
		if kind == "choice":
			selected = answer.get("choice")
			if not isinstance(selected, str) or selected not in options:
				raise ValueError("Response choice is not one of the supplied options.")
			if probabilities[selected] < max(probabilities.values()):
				raise ValueError("Response choice must have the highest probability.")
		else:
			value = answer.get("score")
			if not _number(value) or not 0 <= value <= len(criteria) - 1:
				raise ValueError("Response score is outside the supplied rubric.")
			legend = answer.get("legend")
			if legend != {str(i): level for i, level in enumerate(criteria)}:
				raise ValueError("Response legend does not match the supplied rubric.")
			expected = sum(int(i) * p for i, p in probabilities.items())
			if not math.isclose(value, expected, abs_tol=0.001 * len(criteria)):
				raise ValueError("Response score does not match its probability distribution.")
	usage = payload.get("usage")
	if usage is not None:
		if not isinstance(usage, dict):
			raise ValueError("Response usage must be an object.")
		for key in ("input_tokens", "output_tokens"):
			value = usage.get(key)
			if value is not None and (type(value) is not int or value < 0):
				raise ValueError("Token usage must be a non-negative integer or null.")
	return payload
