import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from flow.jobs import jobs


class TestFlowJob(IntegrationTestCase):
	def setUp(self):
		jobs.setup()
		self.names = []
		self.settings = frappe.get_doc("Flow Settings").as_dict()
		self.user = frappe.session.user

	def tearDown(self):
		frappe.set_user(self.user)
		frappe.db.rollback()
		if self.names:
			frappe.db.delete("Flow Job", {"name": ["in", self.names]})
		settings = frappe.get_doc("Flow Settings")
		for field in (
			"dispatch_enabled",
			"queue_name",
			"max_in_flight",
			"job_timeout",
			"decision_max_attempts",
			"retention_days",
			"failed_retention_days",
		):
			settings.set(field, self.settings[field])
		settings.save(ignore_permissions=True)
		frappe.db.commit()

	def job(self, key=None, state="test"):
		name = jobs._register(
			"Decision", {"decision": "fixture", "state": state}, run_as=self.user, idempotency_key=key
		)
		self.names.append(name)
		return name

	def test_bulk_4000_jobs_use_no_redis_or_enqueue(self):
		with (
			patch("frappe.enqueue") as enqueue,
			patch("flow.jobs.jobs._rq_status") as redis_status,
			patch("frappe.publish_realtime") as realtime,
		):
			for i in range(4000):
				self.job(state=str(i))
		self.assertEqual(len(set(self.names)), 4000)
		self.assertEqual(frappe.db.count("Flow Job", {"name": ["in", self.names], "status": "Pending"}), 4000)
		enqueue.assert_not_called()
		redis_status.assert_not_called()
		realtime.assert_not_called()

	def test_registration_rolls_back_with_application_transaction(self):
		frappe.db.savepoint("before_flow_submission")
		name = self.job()
		frappe.db.rollback(save_point="before_flow_submission")
		self.assertFalse(frappe.db.exists("Flow Job", name))

	def test_idempotency_reuses_identical_input_and_rejects_changed_input(self):
		key = frappe.generate_hash()
		first = self.job(key=key)
		self.assertEqual(self.job(key=key), first)
		with self.assertRaises(ValueError):
			self.job(key=key, state="different")
		self.assertEqual(json.loads(frappe.db.get_value("Flow Job", first, "payload"))["state"], "test")

	def test_concurrent_idempotency_with_full_transaction_retry(self):
		# A database deadlock aborts the caller's whole transaction, never only its outbox row.
		for changed in (False, True):
			key = frappe.generate_hash()
			barrier = Barrier(2)
			site, sites_path, user = frappe.local.site, frappe.local.sites_path, self.user
			frappe.db.commit()

			def register(index):
				try:
					frappe.init(site, sites_path=sites_path)
					frappe.connect()
					frappe.set_user(user)
					for attempt in range(3):
						try:
							frappe.db.count("Flow Job")  # Establish both old read snapshots.
							if attempt == 0:
								barrier.wait(timeout=15)
							name = jobs._register(
								"Decision",
								{"state": str(index) if changed else "same"},
								run_as=user,
								idempotency_key=key,
							)
							frappe.db.commit()
							return name
						except frappe.QueryDeadlockError:
							frappe.db.rollback()
						except ValueError:
							frappe.db.rollback()
							return "conflict"
					raise AssertionError("Transaction contention did not converge")
				finally:
					frappe.destroy()

			with ThreadPoolExecutor(max_workers=2) as pool:
				results = list(pool.map(register, (0, 1)))
			names = {name for name in results if name != "conflict"}
			self.names.extend(names)
			self.assertEqual(len(names), 1)
			self.assertEqual(results.count("conflict"), int(changed))
			frappe.db.rollback()
			self.assertEqual(frappe.db.count("Flow Job", {"name": ["in", list(names)]}), 1)

	def test_registration_does_not_commit(self):
		with patch.object(frappe.db, "commit") as commit:
			self.job()
		commit.assert_not_called()

	def test_job_payload_cannot_be_edited_through_document_api(self):
		name = self.job()
		doc = frappe.get_doc("Flow Job", name)
		doc.run_as = "Guest"
		with self.assertRaises(frappe.PermissionError):
			doc.save(ignore_permissions=True)

	def test_guest_cannot_submit_or_read(self):
		name = self.job()
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			jobs.get_job(name)
		with self.assertRaises(frappe.PermissionError):
			jobs.submit_decision("fixture", state="test")

	def test_two_workers_only_one_claims(self):
		name = self.job()
		jobs._update(name, {"status": "Queued", "reservation": "claim-token"})
		frappe.db.commit()
		site, sites_path = frappe.local.site, frappe.local.sites_path

		def claim():
			try:
				frappe.init(site, sites_path=sites_path)
				frappe.connect()
				return jobs._claim(name, "claim-token") is not None
			finally:
				frappe.destroy()

		with ThreadPoolExecutor(max_workers=2) as pool:
			results = list(pool.map(lambda _: claim(), range(2)))
		self.assertEqual(sorted(results), [False, True])
		frappe.db.rollback()
		self.assertEqual(frappe.db.get_value("Flow Job", name, "attempts"), 1)

	def test_two_dispatchers_respect_capacity(self):
		settings = frappe.get_doc("Flow Settings")
		settings.max_in_flight = 2
		settings.dispatch_enabled = 1
		settings.save()
		for _ in range(6):
			self.job()
		frappe.db.commit()
		site, sites_path = frappe.local.site, frappe.local.sites_path

		def dispatch():
			try:
				frappe.init(site, sites_path=sites_path)
				frappe.connect()
				return jobs.dispatch()
			finally:
				frappe.destroy()

		with patch("frappe.enqueue") as enqueue, ThreadPoolExecutor(max_workers=2) as pool:
			list(pool.map(lambda _: dispatch(), range(2)))
		self.assertEqual(enqueue.call_count, 2)
		frappe.db.rollback()
		self.assertEqual(frappe.db.count("Flow Job", {"name": ["in", self.names], "status": "Queued"}), 2)

	def test_cleanup_uses_finished_at_and_never_deletes_incomplete_work(self):
		old = now_datetime() - timedelta(days=100)
		kept = []
		for status in ("Pending", "Queued", "Running", "Paused", "Completed", "Failed", "Cancelled"):
			name = self.job()
			jobs._update(name, {"status": status, "creation": old, "finished_at": old})
			if status not in jobs.TERMINAL:
				kept.append(name)
		recent = self.job()
		jobs._update(recent, {"status": "Completed", "creation": old, "finished_at": now_datetime()})
		kept.append(recent)
		jobs.cleanup()
		self.assertEqual(
			set(frappe.get_all("Flow Job", filters={"name": ["in", self.names]}, pluck="name")), set(kept)
		)

	def test_trigger_event_and_version_are_distinct_keys(self):
		# Trigger submission's key contains definition + document + event + revision.
		keys = [
			jobs._json(["trigger", "ToDo", "doc", event, version])
			for event, version in (("after_insert", "v1"), ("on_update", "v1"), ("on_update", "v2"))
		]
		self.assertEqual(len({self.job(key=key) for key in keys}), 3)
