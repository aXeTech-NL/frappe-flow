# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

from unittest import TestCase
from unittest.mock import MagicMock, call, patch

from flow.patches import grant_flow_role
from flow.permissions import FLOW_USER_ROLE


class TestGrantFlowRolePatch(TestCase):
	def test_grants_enabled_system_managers_only(self):
		system_managers = [
			"enabled-manager@example.com",
			"disabled-manager@example.com",
			"Administrator",
		]
		get_all_results = [
			system_managers,
			["enabled-manager@example.com", "Administrator"],
			[],
		]
		doc = MagicMock()

		with (
			patch.object(grant_flow_role, "ensure_flow_role") as ensure_role,
			patch.object(grant_flow_role.frappe, "get_all", side_effect=get_all_results) as get_all,
			patch.object(grant_flow_role.frappe, "get_doc", return_value=doc) as get_doc,
			patch.object(grant_flow_role.frappe, "db") as db,
		):
			grant_flow_role.execute()

		ensure_role.assert_called_once_with()
		filters = get_all.call_args_list[1].kwargs["filters"]
		self.assertEqual(filters["enabled"], 1)
		self.assertEqual(filters["user_type"], "System User")
		self.assertEqual(filters["name"], ["in", system_managers])
		get_doc.assert_called_once_with("User", "enabled-manager@example.com")
		doc.add_roles.assert_called_once_with(FLOW_USER_ROLE)
		db.release_savepoint.assert_called_once_with("flow_grant_role_0")
		db.rollback.assert_not_called()

	def test_failure_is_rolled_back_without_stopping_other_users(self):
		failed = MagicMock()
		failed.add_roles.side_effect = RuntimeError("unrelated validation rejected user")
		succeeded = MagicMock()
		docs = {
			"a-failed@example.com": failed,
			"z-succeeded@example.com": succeeded,
		}

		with (
			patch.object(grant_flow_role, "ensure_flow_role"),
			patch.object(
				grant_flow_role.frappe,
				"get_all",
				side_effect=[list(docs), list(docs), []],
			),
			patch.object(
				grant_flow_role.frappe,
				"get_doc",
				side_effect=lambda _doctype, user: docs[user],
			),
			patch.object(grant_flow_role.frappe, "clear_messages") as clear_messages,
			patch.object(grant_flow_role.frappe, "db") as db,
			patch("builtins.print") as print_message,
		):
			grant_flow_role.execute()

		failed.add_roles.assert_called_once_with(FLOW_USER_ROLE)
		succeeded.add_roles.assert_called_once_with(FLOW_USER_ROLE)
		self.assertEqual(
			db.savepoint.call_args_list,
			[call("flow_grant_role_0"), call("flow_grant_role_1")],
		)
		db.rollback.assert_called_once_with(save_point="flow_grant_role_0")
		db.release_savepoint.assert_called_once_with("flow_grant_role_1")
		clear_messages.assert_called_once_with()
		self.assertIn("a-failed@example.com", print_message.call_args.args[0])

	def test_already_granted_users_make_reruns_idempotent(self):
		users = ["existing-manager@example.com"]
		with (
			patch.object(grant_flow_role, "ensure_flow_role") as ensure_role,
			patch.object(
				grant_flow_role.frappe,
				"get_all",
				side_effect=[users, users, users, users, users, users],
			),
			patch.object(grant_flow_role.frappe, "get_doc") as get_doc,
			patch.object(grant_flow_role.frappe, "db") as db,
		):
			grant_flow_role.execute()
			grant_flow_role.execute()

		self.assertEqual(ensure_role.call_count, 2)
		get_doc.assert_not_called()
		db.savepoint.assert_not_called()
