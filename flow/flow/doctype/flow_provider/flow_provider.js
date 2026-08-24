// Copyright (c) 2026, Frappe Technologies and contributors
// License: MIT. See LICENSE

frappe.ui.form.on("Flow Provider", {
	refresh(frm) {
		const field = frm.fields_dict.provider;
		field.df.ignore_validation = true;
		frappe.call({
			method: "flow.flow.doctype.flow_provider.flow_provider.get_connectors",
			callback: ({ message }) => field.set_data(message || ["Custom"]),
		});
	},
});
