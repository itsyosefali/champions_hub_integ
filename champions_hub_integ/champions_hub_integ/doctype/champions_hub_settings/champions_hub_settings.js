frappe.ui.form.on("Champions Hub Settings", {
	test_connection_btn: function (frm) {
		frappe.call({
			method: "champions_hub_integ.api.test_connection",
			freeze: true,
			freeze_message: __("Testing connection..."),
			callback: function (r) {
				if (r.message && r.message.success) {
					frappe.msgprint({
						title: __("Connection Successful"),
						indicator: "green",
						message: __("Connected to Champions Hub. Total enrollments: {0}", [
							r.message.total,
						]),
					});
				} else {
					frappe.msgprint({
						title: __("Connection Failed"),
						indicator: "red",
						message: r.message ? r.message.error : __("Unknown error"),
					});
				}
			},
		});
	},

	sync_now_btn: function (frm) {
		frappe.call({
			method: "champions_hub_integ.api.trigger_sync",
			freeze: true,
			freeze_message: __("Enqueuing sync job..."),
			callback: function (r) {
				frappe.msgprint({
					title: __("Sync Enqueued"),
					indicator: "blue",
					message: __(
						"The sync job has been enqueued and will run in the background. Check the Enrollment Log for results."
					),
				});
			},
		});
	},
});
