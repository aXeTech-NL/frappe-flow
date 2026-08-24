# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""Access control for Flow.

Flow is gated by a single role, `Flow User`. An agent acts within the running
user's own permissions, so this gate controls who may invoke Flow at all.
Enforcement belongs on the whitelisted API boundary; trusted server-side calls,
including triggers, deliberately remain ungated.
"""

from __future__ import annotations

import frappe

FLOW_USER_ROLE = "Flow User"


def assert_flow_access() -> None:
	"""Raise unless the current user may use Flow."""
	frappe.only_for(FLOW_USER_ROLE, message=True)


def has_flow_access(user: str | None = None) -> bool:
	"""Return whether a user may use Flow; this is a UI hint, not the API gate."""
	if (user or frappe.session.user) == "Administrator":
		return True
	return FLOW_USER_ROLE in frappe.get_roles(user)


def ensure_flow_role() -> None:
	"""Create the duplicate-safe Flow User role when it is missing."""
	if frappe.db.exists("Role", FLOW_USER_ROLE):
		return
	frappe.get_doc(
		{
			"doctype": "Role",
			"role_name": FLOW_USER_ROLE,
			"desk_access": 1,
		}
	).insert(ignore_permissions=True, ignore_if_duplicate=True)
