import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


CUSTOM_FIELDS = {
    "Customer": [
        {
            "fieldname": "champions_hub_user_id",
            "label": "Champions Hub User ID",
            "fieldtype": "Data",
            "insert_after": "customer_name",
            "unique": 1,
            "read_only": 1,
            "no_copy": 1,
        }
    ],
    "Sales Invoice": [
        {
            "fieldname": "champions_enrollment_id",
            "label": "Champions Enrollment ID",
            "fieldtype": "Data",
            "insert_after": "naming_series",
            "unique": 1,
            "read_only": 1,
            "no_copy": 1,
        }
    ],
    "Payment Entry": [
        {
            "fieldname": "champions_enrollment_id",
            "label": "Champions Enrollment ID",
            "fieldtype": "Data",
            "insert_after": "naming_series",
            "unique": 1,
            "read_only": 1,
            "no_copy": 1,
        }
    ],
}


def after_install():
    create_custom_fields(CUSTOM_FIELDS, update=True)


def before_uninstall():
    for dt, fields in CUSTOM_FIELDS.items():
        for field in fields:
            fname = f"{dt}-{field['fieldname']}"
            if frappe.db.exists("Custom Field", fname):
                frappe.delete_doc("Custom Field", fname, force=True)
