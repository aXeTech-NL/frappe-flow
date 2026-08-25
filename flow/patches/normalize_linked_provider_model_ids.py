# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

import frappe


def execute():
	"""Store remote model IDs without the linked Provider's internal connector prefix."""
	if not frappe.db.table_exists("Flow Provider") or not frappe.db.table_exists("Flow Model"):
		return

	from flow.flow.doctype.flow_provider.flow_provider import connector_id, strip_connector_prefix

	connections = frappe.get_all("Flow Provider", fields=["name", "provider"])
	for connection in connections:
		connector = connector_id(connection.provider)
		models = frappe.get_all(
			"Flow Model",
			filters={"provider": connection.name},
			fields=["name", "model_id"],
		)
		for model in models:
			remote_model_id = strip_connector_prefix(model.model_id, connector)
			if remote_model_id == model.model_id:
				continue
			frappe.db.set_value(
				"Flow Model",
				model.name,
				"model_id",
				remote_model_id,
				update_modified=False,
			)
