const ACCOUNT_FIELDS = [
	"income_account",
	"fee_expense_account",
	"receivable_account_egp",
	"receivable_account_usd",
	"receivable_account_eur",
	"payment_account_stripe",
	"payment_account_xpay",
	"payment_account_instapay",
	"payment_account_vodafone_cash",
	"payment_account_manual_transfer",
];

frappe.ui.form.on("Champions Hub Settings", {
	onload(frm) {
		ACCOUNT_FIELDS.forEach((field) => {
			frm.set_query(field, () => ({
				filters: {
					company: frm.doc.default_company || "",
					is_group: 0,
					disabled: 0,
				},
			}));
		});

		frm.set_query("cost_center", () => ({
			filters: {
				company: frm.doc.default_company || "",
				is_group: 0,
				disabled: 0,
			},
		}));
	},

	default_company(frm) {
		ACCOUNT_FIELDS.concat(["cost_center"]).forEach((field) => {
			frm.set_value(field, null);
		});
	},

	test_connection_btn(frm) {
		frappe.call({
			method: "champions_hub_integ.api.test_connection",
			freeze: true,
			freeze_message: __("Testing connection..."),
			callback(r) {
				if (r.message?.success) {
					frappe.show_alert({
						message: __("Connected — {0} enrollments", [r.message.total]),
						indicator: "green",
					});
				} else {
					frappe.msgprint({
						title: __("Connection Failed"),
						indicator: "red",
						message: r.message?.error || __("Unknown error"),
					});
				}
			},
		});
	},

	sync_now_btn(frm) {
		frappe.call({
			method: "champions_hub_integ.api.trigger_sync",
			freeze: true,
			freeze_message: __("Enqueuing sync..."),
			callback() {
				frappe.show_alert({
					message: __("Sync enqueued — check Enrollment Log"),
					indicator: "blue",
				});
			},
		});
	},
});
