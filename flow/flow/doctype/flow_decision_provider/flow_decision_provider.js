frappe.ui.form.on("Flow Decision Provider", {
	refresh(frm) {
		if (frm.is_new() || !frm.perm?.[0]?.write) return;
		frm.add_custom_button(__("Test Connection"), async () => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save the document before testing the connection."));
				return;
			}
			frappe.dom.freeze(__("Testing connection…"));
			try {
				const { message } = await frm.call("test_connection");
				if (message && message.ok) {
					frappe.show_alert({ message: message.message, indicator: "green" });
				}
			} finally {
				frappe.dom.unfreeze();
			}
		});
	},
});
