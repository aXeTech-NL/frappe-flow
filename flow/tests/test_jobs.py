"""Queue policy/worker tests with mocked Frappe I/O; database tests live beside Flow Job."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from flow.decisions import DecisionError
from flow.jobs import jobs


class TestJobs(TestCase):
	def setUp(self):
		self.frappe = MagicMock()
		self.frappe.session.user = "worker@example.com"
		self.frappe.PermissionError = PermissionError
		self.frappe.QueryDeadlockError = type("QueryDeadlockError", (Exception,), {})
		self.frappe.flags = {}

		# Frappe's _dict supports both styles.
		class Flags(dict):
			__getattr__ = dict.get
			__setattr__ = dict.__setitem__

		self.frappe.flags = Flags()
		self.frappe.db.get_value.return_value = True
		p = patch.object(jobs, "frappe", self.frappe)
		p.start()
		self.addCleanup(p.stop)
		self.now = datetime(2026, 9, 23, 12)
		p = patch.object(jobs, "now_datetime", return_value=self.now)
		p.start()
		self.addCleanup(p.stop)
		self.ownership_patch = patch.object(jobs, "_owns_reservation", return_value=True)
		self.ownership_patch.start()
		self.addCleanup(self.ownership_patch.stop)
		self.settings = SimpleNamespace(
			dispatch_enabled=1,
			max_in_flight=4,
			queue_name="flow",
			job_timeout=900,
			decision_max_attempts=3,
			retention_days=30,
			failed_retention_days=90,
		)

	def row(self, **values):
		return SimpleNamespace(
			name="job-1",
			status="Queued",
			reservation="token",
			attempts=0,
			job_type="Decision",
			run_as="user@example.com",
			max_attempts=3,
			payload='{"decision":"triage","state":"test"}',
			**values,
		)

	def test_claim_commits_before_execution_and_counts_attempt_once(self):
		row = self.row()
		self.frappe.get_doc.return_value = row
		self.assertIs(jobs._claim("job-1", "token"), row)
		self.frappe.get_doc.assert_called_once_with("Flow Job", "job-1", for_update=True)
		self.frappe.db.commit.assert_called_once()
		self.assertEqual(row.attempts, 1)
		self.assertEqual(row.status, "Running")
		self.assertIsNone(jobs._claim("job-1", "token"))
		self.assertEqual(self.frappe.db.commit.call_count, 1)

	def test_stale_delivery_cannot_claim(self):
		self.frappe.get_doc.return_value = self.row()
		self.assertIsNone(jobs._claim("job-1", "old-token"))
		self.frappe.db.set_value.assert_not_called()
		self.frappe.db.commit.assert_not_called()

	def test_dispatch_caps_queued_plus_running_and_commits_before_publish(self):
		self.frappe.db.count.return_value = 2
		self.frappe.get_all.return_value = [SimpleNamespace(name="j1"), SimpleNamespace(name="j2")]
		self.frappe.get_doc.side_effect = [
			SimpleNamespace(name="j1", status="Pending", job_type="Decision"),
			SimpleNamespace(name="j2", status="Pending", job_type="Agent"),
		]
		self.frappe.enqueue.side_effect = lambda *args, **kwargs: self.assertGreater(
			self.frappe.db.commit.call_count, 0
		)
		with (
			patch.object(jobs, "_settings_lock", return_value=self.settings),
			patch.object(jobs, "_reconcile"),
		):
			self.assertEqual(jobs.dispatch(), 2)
		self.assertEqual(self.frappe.get_all.call_args.kwargs["limit_page_length"], 2)
		self.assertEqual(self.frappe.enqueue.call_count, 2)
		for call in self.frappe.enqueue.call_args_list:
			self.assertEqual(call.kwargs["queue"], "flow")
			self.assertNotIn("payload", call.kwargs)

	def test_full_capacity_does_not_scan_or_enqueue_pending_jobs(self):
		self.frappe.db.count.return_value = 4
		with (
			patch.object(jobs, "_settings_lock", return_value=self.settings),
			patch.object(jobs, "_reconcile"),
		):
			self.assertEqual(jobs.dispatch(), 0)
		self.frappe.get_all.assert_not_called()
		self.frappe.enqueue.assert_not_called()

	def test_pause_still_reconciles_without_publishing(self):
		self.settings.dispatch_enabled = 0
		with (
			patch.object(jobs, "_settings_lock", return_value=self.settings),
			patch.object(jobs, "_reconcile") as recover,
		):
			jobs.dispatch()
		recover.assert_called_once()
		self.frappe.enqueue.assert_not_called()

	def test_publication_failure_leaves_committed_fenced_reservation(self):
		self.frappe.db.count.return_value = 3
		self.frappe.get_all.return_value = [SimpleNamespace(name="j1")]
		self.frappe.get_doc.return_value = SimpleNamespace(name="j1", status="Pending", job_type="Decision")
		self.frappe.enqueue.side_effect = ConnectionError("Redis unavailable")
		with (
			patch.object(jobs, "_settings_lock", return_value=self.settings),
			patch.object(jobs, "_reconcile"),
		):
			jobs.dispatch()
		self.assertEqual(self.frappe.db.commit.call_count, 2)
		values = self.frappe.db.set_value.call_args.args[2]
		self.assertEqual(values["status"], "Queued")
		self.assertTrue(values["reservation"])

	def _recovery_row(self, status):
		return SimpleNamespace(
			name="j1",
			status=status,
			reserved_at=self.now - timedelta(hours=2),
			started_at=self.now - timedelta(hours=2),
			rq_job_id="rq-id",
			timeout=900,
			flow_run="run-1",
		)

	def test_missing_unstarted_delivery_is_fenced_and_retried(self):
		self.frappe.get_all.side_effect = [[self._recovery_row("Queued")], []]
		self.frappe.get_doc.return_value = self._recovery_row("Queued")
		with patch.object(jobs, "_rq_status", return_value=None):
			jobs._reconcile(self.now)
		values = self.frappe.db.set_value.call_args.args[2]
		self.assertEqual(values["status"], "Pending")
		self.assertIsNone(values["reservation"])

	def test_running_worker_is_not_reclaimed_even_when_old(self):
		self.frappe.get_all.side_effect = [[self._recovery_row("Running")], []]
		self.frappe.get_doc.return_value = self._recovery_row("Running")
		with patch.object(jobs, "_rq_status", return_value="started"):
			jobs._reconcile(self.now)
		self.frappe.db.set_value.assert_not_called()

	def test_uncertain_running_work_fails_without_retry(self):
		self.frappe.get_all.side_effect = [[self._recovery_row("Running")], []]
		self.frappe.get_doc.return_value = self._recovery_row("Running")
		with patch.object(jobs, "_rq_status", return_value=None):
			jobs._reconcile(self.now)
		self.assertEqual(self.frappe.db.set_value.call_args.args[2]["status"], "Failed")

	def test_redis_outage_during_recovery_does_not_assume_job_missing(self):
		self.frappe.get_all.side_effect = [[self._recovery_row("Running")], []]
		self.frappe.get_doc.return_value = self._recovery_row("Running")
		with (
			patch.object(jobs, "_rq_status", side_effect=ConnectionError),
			self.assertRaises(ConnectionError),
		):
			jobs._reconcile(self.now)
		self.frappe.db.set_value.assert_not_called()

	def test_paused_run_is_reconciled_when_resumed_to_completion(self):
		self.frappe.get_all.side_effect = [[], [self._recovery_row("Paused")]]
		self.frappe.get_doc.return_value = self._recovery_row("Paused")
		self.frappe.db.get_value.return_value = "Completed"
		jobs._reconcile(self.now)
		self.assertEqual(self.frappe.db.set_value.call_args.args[2]["status"], "Completed")

	def test_decision_transient_failure_gets_backoff_not_sleep(self):
		row = self.row()
		row.attempts = 1
		with (
			patch.object(jobs, "_claim", return_value=row),
			patch.object(jobs, "dispatch"),
			patch("flow.decisions.evaluate", side_effect=DecisionError("limited", kind="rate_limit")),
		):
			jobs.run(row.name, "token")
		values = self.frappe.db.set_value.call_args.args[2]
		self.assertEqual(values["status"], "Pending")
		self.assertEqual(values["next_attempt_at"], self.now + timedelta(seconds=30))
		self.frappe.set_user.assert_any_call("worker@example.com")

	def test_exhausted_or_permanent_decisions_fail(self):
		for kind, attempts in (("rate_limit", 3), ("authentication", 1), ("invalid_response", 1)):
			row = self.row()
			row.attempts = attempts
			with (
				self.subTest(kind=kind),
				patch.object(jobs, "_claim", return_value=row),
				patch.object(jobs, "dispatch"),
				patch("flow.decisions.evaluate", side_effect=DecisionError("safe", kind=kind)),
			):
				jobs.run(row.name, "token")
			self.assertEqual(self.frappe.db.set_value.call_args.args[2]["status"], "Failed")

	def test_agent_exception_is_not_retried_or_echoed(self):
		row = self.row()
		row.job_type = "Agent"
		row.payload = json.dumps({"agent": "agent", "input": "private", "auto_approve": True})
		self.frappe.get_doc.return_value.run.side_effect = RuntimeError("credential-secret")
		with patch.object(jobs, "_claim", return_value=row), patch.object(jobs, "dispatch"):
			jobs.run(row.name, "token")
		values = self.frappe.db.set_value.call_args.args[2]
		self.assertEqual(values["status"], "Failed")
		self.assertNotIn("credential-secret", values["error"])

	def test_disabled_execution_user_never_calls_provider(self):
		self.frappe.db.get_value.return_value = False
		with (
			patch.object(jobs, "_claim", return_value=self.row()),
			patch.object(jobs, "dispatch"),
			patch("flow.decisions.evaluate") as evaluate,
		):
			jobs.run("job-1", "token")
		evaluate.assert_not_called()
		self.assertEqual(self.frappe.db.set_value.call_args.args[2]["status"], "Failed")

	def test_cleanup_filters_terminal_finished_at_and_batches(self):
		self.frappe.get_doc.return_value = self.settings
		self.frappe.get_all.side_effect = [["completed"], ["failed"]]
		self.frappe.utils.create_batch.side_effect = lambda rows, size: [rows]
		self.assertEqual(jobs.cleanup(), 2)
		for call in self.frappe.get_all.call_args_list:
			filters = call.kwargs["filters"]
			self.assertIn("finished_at", filters)
			self.assertNotIn("creation", filters)
			self.assertTrue(set(filters["status"][1]) <= set(jobs.TERMINAL))
			self.assertEqual(call.kwargs["limit_page_length"], 1000)

	def test_zero_retention_keeps_terminal_jobs(self):
		self.settings.retention_days = 0
		self.settings.failed_retention_days = 0
		self.frappe.get_doc.return_value = self.settings
		self.assertEqual(jobs.cleanup(), 0)
		self.frappe.db.delete.assert_not_called()

	def test_outcome_deadlock_retries_database_not_provider(self):
		row = self.row()
		self.frappe.db.set_value.side_effect = [self.frappe.QueryDeadlockError(), None]
		with (
			patch.object(jobs, "_claim", return_value=row),
			patch.object(jobs, "dispatch"),
			patch("flow.decisions.evaluate", return_value={"model": "test", "answers": {}}) as evaluate,
		):
			jobs.run(row.name, "token")
		evaluate.assert_called_once()
		self.assertEqual(self.frappe.db.set_value.call_count, 2)
		self.assertEqual(self.frappe.db.set_value.call_args.args[2]["status"], "Completed")
		self.frappe.db.rollback.assert_called_once()

	def test_claim_deadlock_retries_before_any_provider_execution(self):
		self.frappe.get_doc.side_effect = [self.frappe.QueryDeadlockError(), self.row()]
		claimed = jobs._claim("job-1", "token")
		self.assertEqual(claimed.attempts, 1)
		self.frappe.db.rollback.assert_called_once()
		self.frappe.db.commit.assert_called_once()

	def test_fresh_active_rows_are_excluded_before_locking(self):
		self.frappe.get_all.side_effect = [[], []]
		jobs._reconcile(self.now)
		filters = self.frappe.get_all.call_args_list[0].kwargs["filters"]
		self.assertEqual(filters["reserved_at"], ["<=", self.now - timedelta(seconds=60)])
		self.frappe.get_doc.assert_not_called()
		self.frappe.db.set_value.assert_not_called()

	def test_other_identity_cannot_read_trigger_result(self):
		self.frappe.get_doc.return_value.run_as = "privileged@example.com"
		self.frappe.get_doc.return_value.check_permission.side_effect = PermissionError
		with self.assertRaises(PermissionError):
			jobs.get_job("privileged-job")
		self.frappe.get_doc.return_value.check_permission.assert_called_once_with("read")

	def test_same_trigger_document_recursion_is_suppressed(self):
		self.frappe.get_doc.return_value.enabled = True
		self.frappe.get_doc.return_value.run_as = "service@example.com"
		self.frappe.flags.flow_job_trigger = ("trigger", "ToDo", "todo-1")
		with patch.object(jobs, "_register") as register:
			self.assertIsNone(
				jobs.submit_trigger(
					trigger="trigger",
					target_doctype="ToDo",
					target_name="todo-1",
					event="on_update",
					version="v2",
				)
			)
		register.assert_not_called()

	def test_late_worker_cannot_overwrite_recovered_outcome(self):
		self.ownership_patch.stop()
		self.frappe.get_doc.return_value = SimpleNamespace(status="Failed", reservation=None)
		action = MagicMock()
		jobs._persist(action, name="job-1", reservation="old-token")
		action.assert_not_called()
		self.frappe.get_doc.assert_called_once_with("Flow Job", "job-1", for_update=True)
		self.frappe.db.commit.assert_called_once()

	def test_recent_claim_is_not_expired_by_long_queue_wait(self):
		row = self._recovery_row("Running")
		row.started_at = self.now - timedelta(seconds=2)
		self.frappe.get_all.side_effect = [[row], []]
		self.frappe.get_doc.return_value = row
		with patch.object(jobs, "_rq_status", return_value=None):
			jobs._reconcile(self.now)
		self.frappe.db.set_value.assert_not_called()

	def test_recovery_preserves_committed_agent_outcome(self):
		for status in ("Paused", "Completed", "Failed"):
			with self.subTest(status=status):
				row = self._recovery_row("Running")
				self.frappe.get_all.side_effect = [[row], []]
				self.frappe.get_doc.return_value = row
				self.frappe.db.get_value.return_value = status
				with patch.object(jobs, "_rq_status", return_value="failed"):
					jobs._reconcile(self.now)
				values = self.frappe.db.set_value.call_args.args[2]
				self.assertEqual(values["status"], status)
				self.assertEqual(values["flow_run"], row.flow_run)
				if status == "Paused":
					self.assertNotIn("finished_at", values)

	def test_registration_has_no_document_notifications_or_commit(self):
		doc = self.frappe.get_doc.return_value
		jobs._register("Decision", {"state": "test"}, run_as="service@example.com")
		doc.db_insert.assert_called_once_with(ignore_if_duplicate=True)
		doc.insert.assert_not_called()
		self.frappe.publish_realtime.assert_not_called()
		self.frappe.db.commit.assert_not_called()
		self.frappe.db.sql.assert_not_called()
		values = self.frappe.get_doc.call_args.args[0]
		self.assertEqual(values["owner"], "service@example.com")

	def test_registration_does_not_mask_or_retry_caller_transaction_conflicts(self):
		self.frappe.get_doc.return_value.db_insert.side_effect = self.frappe.QueryDeadlockError()
		with self.assertRaises(self.frappe.QueryDeadlockError):
			jobs._register("Decision", {}, run_as="service@example.com", idempotency_key="same")
		self.frappe.db.sql.assert_not_called()
		self.frappe.db.commit.assert_not_called()
		self.frappe.get_doc.return_value.db_insert.assert_called_once()

	def test_nested_agent_run_does_not_replace_root_job_link(self):
		self.frappe.flags.flow_background_job = "job-1"
		self.frappe.db.get_value.return_value = "root-run"
		jobs.attach_run("nested-run")
		self.frappe.db.set_value.assert_not_called()

	def _resume_fixture(self):
		job = SimpleNamespace(
			name="job-1",
			run_as="service@example.com",
			job_type="Trigger",
			payload=json.dumps({"trigger": "trigger", "target_doctype": "ToDo", "target_name": "todo-1"}),
		)
		self.frappe.db.get_value.side_effect = [job, True]
		self.frappe.set_user.side_effect = lambda user: setattr(self.frappe.session, "user", user)
		agent = SimpleNamespace(enabled=True, check_permission=MagicMock())
		trigger = SimpleNamespace(enabled=True, run_as=job.run_as)
		self.frappe.get_doc.side_effect = [agent, trigger]
		for target in ("_assert_session_owner", "assert_run_owner", "_resolve_existing_agent"):
			p = patch("flow.lib.session." + target)
			mock = p.start()
			self.addCleanup(p.stop)
			if target == "_resolve_existing_agent":
				mock.return_value = (MagicMock(), {})
		return SimpleNamespace(name="run-1", source="Trigger"), SimpleNamespace(agent="agent-1"), trigger

	def test_background_resume_keeps_identity_and_recursion_guard(self):
		run, session, _ = self._resume_fixture()
		with jobs.resume_context(run, session):
			self.assertEqual(self.frappe.session.user, "service@example.com")
			self.assertEqual(self.frappe.flags.flow_background_job, "job-1")
			self.assertEqual(self.frappe.flags.flow_job_trigger, ("trigger", "ToDo", "todo-1"))
		self.assertEqual(self.frappe.session.user, "worker@example.com")
		self.assertIsNone(self.frappe.flags.flow_job_trigger)

	def test_background_resume_rejects_changed_identity_and_restores_context(self):
		run, session, trigger = self._resume_fixture()
		trigger.run_as = "other@example.com"
		with self.assertRaises(PermissionError), jobs.resume_context(run, session):
			self.fail("Changed trigger identity must not resume")
		self.assertEqual(self.frappe.session.user, "worker@example.com")
		self.assertIsNone(self.frappe.flags.flow_background_job)

	def test_streamed_resume_context_covers_consumption_and_close(self):
		from flow.flow.doctype.flow_session.flow_session import FlowSession

		run, session, _ = self._resume_fixture()

		def events(*args, **kwargs):
			self.assertEqual(self.frappe.session.user, "service@example.com")
			yield "event"

		session._resume_run = events
		stream = FlowSession._resume_stream(session, run, {})
		self.assertEqual(self.frappe.session.user, "worker@example.com")
		self.assertEqual(next(stream), "event")
		stream.close()
		self.assertEqual(self.frappe.session.user, "worker@example.com")
		self.assertIsNone(self.frappe.flags.flow_job_trigger)

	def test_retry_delay_is_bounded(self):
		self.assertEqual(jobs.retry_delay(1), 30)
		self.assertEqual(jobs.retry_delay(2), 60)
		self.assertEqual(jobs.retry_delay(1000), 3600)
