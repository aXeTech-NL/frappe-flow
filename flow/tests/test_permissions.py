# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from flow.api import (
	attach_file,
	get_agent_tools,
	recover_session,
	resume_run,
	start_run,
	stop_run,
	submit_feedback,
)
from flow.boot import boot_session
from flow.lib.model import ChatResponse, Model
from flow.permissions import FLOW_USER_ROLE, ensure_flow_role, has_flow_access
from flow.tools.builtins import sync_builtin_tools


def _final(text: str = "done") -> ChatResponse:
	return ChatResponse(
		content=text,
		finish_reason="stop",
		usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
	)


def _user(email: str, roles: list[str]) -> str:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
			}
		).insert(ignore_permissions=True)
	doc = frappe.get_doc("User", email)
	doc.set("roles", [])
	doc.save(ignore_permissions=True)
	if roles:
		doc.add_roles(*roles)
	return email


class TestFlowRoleGate(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		sync_builtin_tools()

	def setUp(self):
		ensure_flow_role()
		self.model = frappe.get_doc(
			{
				"doctype": "Flow Model",
				"title": "Permission Test Model",
				"model_id": "openai/gpt-4o-mini",
				"enabled": 1,
			}
		).insert()
		self.agent = frappe.get_doc(
			{
				"doctype": "Flow Agent",
				"title": "Permission Test Agent",
				"model": self.model.name,
				"instructions": "Be terse.",
				"enabled": 1,
			}
		).insert()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_role_is_created_with_desk_access(self):
		self.assertTrue(frappe.db.exists("Role", FLOW_USER_ROLE))
		self.assertTrue(frappe.db.get_value("Role", FLOW_USER_ROLE, "desk_access"))

	def test_role_creation_is_idempotent(self):
		ensure_flow_role()
		ensure_flow_role()
		self.assertEqual(frappe.db.count("Role", {"name": FLOW_USER_ROLE}), 1)

	def test_user_without_role_is_blocked(self):
		frappe.set_user(_user("flow-no-role@example.com", ["Desk User"]))
		with self.assertRaises(frappe.PermissionError):
			start_run("hi", agent=self.agent.name)

	def test_system_manager_without_role_is_blocked(self):
		frappe.set_user(_user("flow-system-manager@example.com", ["System Manager"]))
		with self.assertRaises(frappe.PermissionError):
			start_run("hi", agent=self.agent.name)

	def test_user_with_role_is_allowed(self):
		frappe.set_user(_user("flow-allowed@example.com", [FLOW_USER_ROLE]))
		with patch.object(Model, "chat", return_value=_final("hello")):
			payload = start_run("hi", agent=self.agent.name)
		self.assertEqual(payload["status"], "Completed")
		self.assertEqual(payload["output"], "hello")

	def test_administrator_is_allowed(self):
		frappe.set_user("Administrator")
		with patch.object(Model, "chat", return_value=_final()):
			self.assertEqual(start_run("hi", agent=self.agent.name)["status"], "Completed")

	def test_every_public_endpoint_checks_access_before_arguments(self):
		import flow.api

		frappe.set_user(_user("flow-endpoint-sweep@example.com", ["Desk User"]))
		calls = {
			"start_run": lambda: start_run("hi"),
			"resume_run": lambda: resume_run("any-run", {}),
			"stop_run": lambda: stop_run("any-run"),
			"recover_session": lambda: recover_session("any-session"),
			"submit_feedback": lambda: submit_feedback("any-run", "Up"),
			"get_agent_tools": lambda: get_agent_tools(self.agent.name),
			"attach_file": lambda: attach_file("any-file"),
		}
		self.assertEqual(sorted(calls), sorted(flow.api.__all__))
		for name, call in calls.items():
			with self.subTest(endpoint=name), self.assertRaises(frappe.PermissionError):
				call()

	def test_unauthorized_start_rejects_before_page_context_processing(self):
		frappe.set_user(_user("flow-context-blocked@example.com", ["Desk User"]))
		with (
			patch("frappe.parse_json") as parse_json,
			patch("flow.lib.page_context.build_page_context") as build_page_context,
			self.assertRaises(frappe.PermissionError),
		):
			start_run("hi", page_context='{"type": "route"}')
		parse_json.assert_not_called()
		build_page_context.assert_not_called()


class TestFlowAccessHelper(IntegrationTestCase):
	def setUp(self):
		ensure_flow_role()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_has_flow_access_reflects_role(self):
		frappe.set_user(_user("flow-helper-no@example.com", ["Desk User"]))
		self.assertFalse(has_flow_access())
		frappe.set_user(_user("flow-helper-yes@example.com", [FLOW_USER_ROLE]))
		self.assertTrue(has_flow_access())

	def test_administrator_has_access(self):
		frappe.set_user("Administrator")
		self.assertTrue(has_flow_access())

	def test_boot_exports_role_flag_and_preserves_file_types(self):
		frappe.set_user(_user("flow-boot-no@example.com", ["Desk User"]))
		bootinfo = frappe._dict()
		boot_session(bootinfo)
		self.assertFalse(bootinfo.flow_enabled)
		self.assertTrue(bootinfo.flow_supported_file_types)

		frappe.set_user(_user("flow-boot-yes@example.com", [FLOW_USER_ROLE]))
		bootinfo = frappe._dict()
		boot_session(bootinfo)
		self.assertTrue(bootinfo.flow_enabled)


class TestTriggersBypassRoleGate(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		sync_builtin_tools()

	def setUp(self):
		ensure_flow_role()
		self.model = frappe.get_doc(
			{
				"doctype": "Flow Model",
				"title": "Trigger Permission Model",
				"model_id": "openai/gpt-4o-mini",
				"enabled": 1,
			}
		).insert()
		self.agent = frappe.get_doc(
			{
				"doctype": "Flow Agent",
				"title": "Trigger Permission Agent",
				"model": self.model.name,
				"instructions": "Be terse.",
				"enabled": 1,
			}
		).insert()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_trigger_runs_when_run_as_user_lacks_flow_role(self):
		from flow.triggers import fire

		runner = _user("flow-trigger-runner@example.com", ["System Manager"])
		self.assertFalse(has_flow_access(runner))
		trigger = frappe.get_doc(
			{
				"doctype": "Flow Trigger",
				"title": "Permission Gate Trigger",
				"agent": self.agent.name,
				"enabled": 1,
				"event": "DocType Event",
				"target_doctype": "ToDo",
				"doc_event": "after_insert",
				"prompt_template": "New {{ doc.doctype }} {{ doc.name }}",
				"run_as": runner,
			}
		).insert()
		todo = frappe.get_doc({"doctype": "ToDo", "description": "permission gate probe"}).insert()

		with patch.object(Model, "chat", return_value=_final("acknowledged")):
			run_name = fire(trigger.name, target_doctype="ToDo", target_name=todo.name)

		self.assertIsNotNone(run_name)
		run = frappe.get_doc("Flow Run", run_name)
		self.assertEqual(run.status, "Completed")
		self.assertEqual(run.owner, runner)
