"""Real permission/session/tool continuation checks; model calls and commits are isolated."""

from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from flow.jobs import jobs
from flow.lib.model import ChatResponse, Model, ToolCall
from flow.lib.session import load_session


def capture_job_identity(todo_name: str) -> dict:
	"""Update the trigger document as the resumed execution identity."""
	doc = frappe.get_doc("ToDo", todo_name)
	doc.description = "Resumed by " + frappe.session.user
	doc.save()
	return {"user": frappe.session.user}


class TestJobResume(IntegrationTestCase):
	def setUp(self):
		self.original_user = frappe.session.user
		frappe.set_user("Administrator")
		self.commit_patch = patch.object(frappe.db, "commit")
		self.commit_patch.start()
		self.addCleanup(self.commit_patch.stop)
		suffix = frappe.generate_hash(length=10)
		self.service = f"flow-job-{suffix}@example.test"
		frappe.get_doc(
			{
				"doctype": "User",
				"email": self.service,
				"first_name": "Flow Job Service",
				"enabled": 1,
				"send_welcome_email": 0,
			}
		).insert()
		model = frappe.get_doc(
			{
				"doctype": "Flow Model",
				"title": "Resume Model " + suffix,
				"model_id": "openai/gpt-4o-mini",
				"enabled": 1,
			}
		).insert()
		tool = frappe.get_doc(
			{
				"doctype": "Flow Tool",
				"title": "Resume Probe " + suffix,
				"description": "Update the ToDo and report the execution identity.",
				"slug": "resume_probe_" + suffix,
				"type": "Imported",
				"enabled": 1,
				"import_path": "flow.flow.doctype.flow_job.test_job_resume.capture_job_identity",
				"requires_confirmation": 1,
			}
		).insert()
		self.agent = frappe.get_doc(
			{
				"doctype": "Flow Agent",
				"title": "Resume Agent " + suffix,
				"model": model.name,
				"enabled": 1,
				"tools": [{"tool": tool.name}],
				"instructions": "Use the probe.",
			}
		).insert()
		self.todo = frappe.get_doc({"doctype": "ToDo", "description": "Before approval"}).insert()
		for doctype, name, write in (
			("Flow Model", model.name, 0),
			("Flow Agent", self.agent.name, 0),
			("ToDo", self.todo.name, 1),
		):
			frappe.share.add(doctype, name, user=self.service, read=1, write=write, notify=0)
		self.trigger = frappe.get_doc(
			{
				"doctype": "Flow Trigger",
				"title": "Resume Trigger " + suffix,
				"agent": self.agent.name,
				"enabled": 1,
				"event": "DocType Event",
				"target_doctype": "ToDo",
				"doc_event": "on_update",
				"run_as": self.service,
				"prompt_template": "Update {{ doc.name }} using the probe.",
				"auto_approve": 0,
			}
		).insert()
		self.job = jobs.submit_trigger(
			trigger=self.trigger.name,
			target_doctype="ToDo",
			target_name=self.todo.name,
			event="on_update",
			version=self.todo.modified,
		)
		jobs._update(self.job, {"status": "Queued", "reservation": "resume-test", "max_attempts": 1})
		response = ChatResponse(
			content=None,
			tool_calls=[
				ToolCall(id="approve-probe", name=tool.slug, arguments={"todo_name": self.todo.name})
			],
			finish_reason="tool_calls",
		)
		with patch.object(Model, "chat", return_value=response), patch.object(jobs, "dispatch"):
			jobs.run(self.job, "resume-test")
		job = frappe.get_doc("Flow Job", self.job)
		self.assertEqual(job.status, "Paused", job.error)
		self.run = frappe.get_doc("Flow Run", job.flow_run)

	def tearDown(self):
		frappe.set_user(self.original_user)
		frappe.db.rollback()

	def test_manager_approval_uses_service_identity_and_does_not_retrigger(self):
		session = load_session(self.run.session)
		with patch.object(Model, "chat", return_value=ChatResponse(content="done")):
			result = session.resume({"approve-probe": "Approve"})
		self.assertEqual(result.status, "Completed")
		self.assertEqual(frappe.session.user, "Administrator")
		self.assertEqual(
			frappe.db.get_value("ToDo", self.todo.name, "description"), "Resumed by " + self.service
		)
		self.assertEqual(frappe.db.count("Flow Job", {"reference_name": self.todo.name}), 1)
		self.assertIsNone(frappe.flags.get("flow_job_trigger"))
		jobs._reconcile(frappe.utils.now_datetime())
		self.assertEqual(frappe.db.get_value("Flow Job", self.job, "status"), "Completed")

	def test_session_retention_preserves_unfinished_background_history(self):
		from flow.flow.doctype.flow_session.flow_session import FlowSession

		frappe.db.set_value(
			"Flow Session",
			self.run.session,
			"modified",
			frappe.utils.now_datetime() - timedelta(days=100),
			update_modified=False,
		)
		FlowSession.clear_old_logs(days=90)
		self.assertTrue(frappe.db.exists("Flow Session", self.run.session))
		self.assertTrue(frappe.db.exists("Flow Run", self.run.name))
		self.assertEqual(frappe.db.get_value("Flow Job", self.job, "status"), "Paused")

	def test_crash_before_job_outcome_preserves_pause_and_later_resume(self):
		jobs._update(
			self.job,
			{
				"status": "Running",
				"reserved_at": frappe.utils.now_datetime() - timedelta(hours=2),
				"started_at": frappe.utils.now_datetime(),
				"timeout": 900,
				"rq_job_id": "lost-job",
				"reservation": "lost-token",
			},
		)
		with patch.object(jobs, "_rq_status", return_value=None):
			jobs._reconcile(frappe.utils.now_datetime())
		job = frappe.get_doc("Flow Job", self.job)
		self.assertEqual(job.status, "Paused")
		self.assertIsNone(job.finished_at)
		self.test_manager_approval_uses_service_identity_and_does_not_retrigger()

	def test_streamed_manager_approval_keeps_service_identity(self):
		session = load_session(self.run.session)

		def response(*args, **kwargs):
			yield "done"
			return ChatResponse(content="done")

		with patch.object(Model, "chat", side_effect=response):
			list(session.resume({"approve-probe": "Approve"}, stream=True))
		self.assertEqual(frappe.session.user, "Administrator")
		self.assertEqual(
			frappe.db.get_value("ToDo", self.todo.name, "description"), "Resumed by " + self.service
		)
		self.assertEqual(frappe.db.count("Flow Job", {"reference_name": self.todo.name}), 1)
		self.assertIsNone(frappe.flags.get("flow_job_trigger"))

	def test_disabled_service_user_cannot_resume(self):
		session = load_session(self.run.session)
		frappe.db.set_value("User", self.service, "enabled", 0)
		with patch.object(Model, "chat") as model, self.assertRaises(frappe.PermissionError):
			session.resume({"approve-probe": "Approve"})
		model.assert_not_called()
		self.assertEqual(frappe.db.get_value("ToDo", self.todo.name, "description"), "Before approval")
		self.assertEqual(frappe.session.user, "Administrator")

	def test_revoked_agent_permission_cannot_resume(self):
		session = load_session(self.run.session)
		frappe.share.remove("Flow Agent", self.agent.name, self.service)
		other_agent = frappe.get_doc(
			{
				"doctype": "Flow Agent",
				"title": "Allowed Agent " + frappe.generate_hash(length=10),
				"instructions": "This is the only agent allowed after revocation.",
				"model": self.agent.model,
				"enabled": 1,
			}
		).insert()
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": self.service,
				"allow": "Flow Agent",
				"for_value": other_agent.name,
				"apply_to_all_doctypes": 1,
			}
		).insert()
		self.assertFalse(frappe.has_permission("Flow Agent", "read", self.agent.name, user=self.service))
		with patch.object(Model, "chat") as model, self.assertRaises(frappe.PermissionError):
			session.resume({"approve-probe": "Approve"})
		model.assert_not_called()
		self.assertEqual(frappe.session.user, "Administrator")

	def test_normal_document_api_cannot_mutate_job(self):
		from frappe.client import set_value

		with self.assertRaises(frappe.PermissionError):
			set_value("Flow Job", self.job, "status", "Completed")
		self.assertEqual(frappe.db.get_value("Flow Job", self.job, "status"), "Paused")

	def test_other_user_cannot_read_privileged_trigger_result(self):
		other = f"flow-other-{frappe.generate_hash(length=10)}@example.test"
		frappe.get_doc(
			{
				"doctype": "User",
				"email": other,
				"first_name": "Unprivileged",
				"enabled": 1,
				"send_welcome_email": 0,
			}
		).insert()
		frappe.set_user(other)
		with self.assertRaises(frappe.PermissionError):
			jobs.get_job(self.job)

	def _assert_rejected_stream(self, response, user="Administrator"):
		body = b"".join(response.response)
		self.assertIn(b"event: error", body)
		self.assertIn(b"Check execution-user permissions", body)
		self.assertEqual(frappe.session.user, user)
		self.assertEqual(frappe.db.get_value("Flow Run", self.run.name, "status"), "Paused")
		self.assertEqual(frappe.db.get_value("ToDo", self.todo.name, "description"), "Before approval")
		self.assertIsNone(frappe.flags.get("flow_job_trigger"))

	def test_streamed_disabled_user_reports_error_without_executing(self):
		from flow.api.api import resume_run

		with patch.object(Model, "chat") as model:
			response = resume_run(self.run.name, {"approve-probe": "Approve"}, stream=True)
			frappe.db.set_value("User", self.service, "enabled", 0)
			self._assert_rejected_stream(response)
		model.assert_not_called()

	def test_streamed_revoked_model_permission_reports_error(self):
		from flow.api.api import resume_run

		with patch.object(Model, "chat") as model:
			response = resume_run(self.run.name, {"approve-probe": "Approve"}, stream=True)
			frappe.share.remove("Flow Model", self.agent.model, self.service)
			other = frappe.get_doc(
				{
					"doctype": "Flow Model",
					"title": "Allowed Model " + frappe.generate_hash(length=10),
					"model_id": "openai/gpt-4o-mini",
					"enabled": 1,
				}
			).insert()
			frappe.get_doc(
				{
					"doctype": "User Permission",
					"user": self.service,
					"allow": "Flow Model",
					"for_value": other.name,
					"apply_to_all_doctypes": 1,
				}
			).insert()
			self.assertFalse(frappe.has_permission("Flow Model", "read", self.agent.model, user=self.service))
			self._assert_rejected_stream(response)
		model.assert_not_called()

	def test_stream_rechecks_approver_before_consumption(self):
		from flow.api.api import resume_run

		response = resume_run(self.run.name, {"approve-probe": "Approve"}, stream=True)
		other = f"flow-approver-{frappe.generate_hash(length=10)}@example.test"
		frappe.get_doc(
			{
				"doctype": "User",
				"email": other,
				"first_name": "Unprivileged",
				"enabled": 1,
				"send_welcome_email": 0,
			}
		).insert()
		frappe.set_user(other)
		with patch.object(Model, "chat") as model:
			self._assert_rejected_stream(response, user=other)
		model.assert_not_called()

	def test_changed_trigger_identity_cannot_resume(self):
		session = load_session(self.run.session)
		frappe.db.set_value("Flow Trigger", self.trigger.name, "run_as", "Administrator")
		with patch.object(Model, "chat") as model, self.assertRaises(frappe.PermissionError):
			session.resume({"approve-probe": "Approve"})
		model.assert_not_called()
		self.assertEqual(frappe.session.user, "Administrator")
