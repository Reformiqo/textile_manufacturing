import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def make_custom_fields():
    """Custom fields shipped by this app. Idempotent -- safe to run on migrate."""
    custom_fields = {
        "Stock Entry": [
            {
                "fieldname": "master_job_card",
                "label": "Master Job Card",
                "fieldtype": "Link",
                "options": "Master Job Card",
                "insert_after": "work_order",
                "read_only": 1,
                "print_hide": 1,
            }
        ],
    }
    create_custom_fields(custom_fields, ignore_validate=True)
