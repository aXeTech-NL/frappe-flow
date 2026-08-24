// Copyright (c) 2026, Frappe Technologies and contributors
// License: MIT. See LICENSE

frappe.ui.form.on("Flow Provider", {
	refresh(frm) {
		load_connectors(frm);
		toggle_operation_fields(frm);
	},

	provider(frm) {
		frm.refresh_field("base_url");
	},

	api_style(frm) {
		toggle_operation_fields(frm);
	},
});

function load_connectors(frm) {
	const field = frm.fields_dict.provider;
	field.df.ignore_validation = true;
	frappe.call({
		method: "flow.flow.doctype.flow_provider.flow_provider.get_connectors",
		callback: ({ message }) => field.set_data(message || ["Custom"]),
	});
}

function toggle_operation_fields(frm) {
	const style = frm.doc.api_style || "Auto";
	frm.toggle_display("chat_base_url", style !== "Responses");
	frm.toggle_display("responses_base_url", style !== "Chat Completions");
}
