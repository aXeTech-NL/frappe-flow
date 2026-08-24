# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

import frappe


def execute():
	"""Give legacy connections a display title without changing their document names."""
	if not frappe.db.table_exists("Flow Provider") or not frappe.db.has_column("Flow Provider", "title"):
		return
	frappe.db.sql(
		"""
		UPDATE `tabFlow Provider`
		SET title = provider
		WHERE COALESCE(title, '') = ''
		"""
	)
