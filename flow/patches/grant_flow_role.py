# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""Grandfather enabled System Managers onto the Flow User role.

Each role grant is isolated because saving a User runs validations installed by
other apps. One unrelated validation failure must not abort the migration.
Fresh installs do not execute historical patches, so they start without grants.
"""

import frappe

from flow.permissions import FLOW_USER_ROLE, ensure_flow_role


def execute() -> None:
	ensure_flow_role()

	system_managers = frappe.get_all(
		"Has Role",
		filters={"role": "System Manager", "parenttype": "User"},
		pluck="parent",
		distinct=True,
	)
	if not system_managers:
		return

	enabled = set(
		frappe.get_all(
			"User",
			filters={"name": ["in", system_managers], "enabled": 1, "user_type": "System User"},
			pluck="name",
		)
	)
	# Administrator bypasses the gate and should not receive a redundant role row.
	enabled.discard("Administrator")
	if not enabled:
		return

	already_granted = set(
		frappe.get_all(
			"Has Role",
			filters={"role": FLOW_USER_ROLE, "parenttype": "User", "parent": ["in", list(enabled)]},
			pluck="parent",
		)
	)

	granted: list[str] = []
	skipped: list[tuple[str, str]] = []
	for index, user in enumerate(sorted(enabled - already_granted)):
		savepoint = f"flow_grant_role_{index}"
		frappe.db.savepoint(savepoint)
		try:
			frappe.get_doc("User", user).add_roles(FLOW_USER_ROLE)
		except Exception as error:
			frappe.db.rollback(save_point=savepoint)
			frappe.clear_messages()
			skipped.append((user, str(error)[:200]))
		else:
			frappe.db.release_savepoint(savepoint)
			granted.append(user)

	if skipped:
		listed = "\n".join(f"- {user}: {reason}" for user, reason in skipped)
		print(
			f"Flow: granted '{FLOW_USER_ROLE}' to {len(granted)} user(s). "
			f"{len(skipped)} could not be granted and will not have Flow access:\n{listed}\n"
			f"Grant the role manually (User > Roles) if any of them should keep it."
		)
