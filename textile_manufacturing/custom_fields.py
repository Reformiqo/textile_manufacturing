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
            },
            {
                "fieldname": "master_work_order",
                "label": "Master Work Order",
                "fieldtype": "Link",
                "options": "Master Work Order",
                "insert_after": "master_job_card",
                "read_only": 1,
                "print_hide": 1,
            },
        ],
        "Job Card": [
            {
                "fieldname": "master_job_card",
                "label": "Master Job Card",
                "fieldtype": "Link",
                "options": "Master Job Card",
                "insert_after": "work_order",
                "read_only": 1,
                "print_hide": 1,
            },
        ],
        "Subcontracting Order": [
            {
                "fieldname": "master_work_order",
                "label": "Master Work Order",
                "fieldtype": "Link",
                "options": "Master Work Order",
                "insert_after": "purchase_order",
                "read_only": 1,
                "print_hide": 1,
            },
            {
                # The row name of the Master Work Order Operation this order is the
                # supplier's half of, so the trail runs Master Work Order ->
                # operation -> item -> Subcontracting Order. Data rather than Link:
                # the operation line is a child row and has no form to link to.
                "fieldname": "master_work_order_operation",
                "label": "Master Work Order Operation",
                "fieldtype": "Data",
                "insert_after": "master_work_order",
                "read_only": 1,
                "print_hide": 1,
            },
        ],
        "Subcontracting Receipt": [
            {
                "fieldname": "master_work_order",
                "label": "Master Work Order",
                "fieldtype": "Link",
                "options": "Master Work Order",
                "insert_after": "supplier",
                "read_only": 1,
                "print_hide": 1,
            }
        ],
        "Purchase Order": [
            {
                "fieldname": "master_work_order",
                "label": "Master Work Order",
                "fieldtype": "Link",
                "options": "Master Work Order",
                "insert_after": "supplier",
                "read_only": 1,
                "print_hide": 1,
            },
            {
                # Which Out House operation line the order was raised for -- see the
                # Subcontracting Order's field of the same name.
                "fieldname": "master_work_order_operation",
                "label": "Master Work Order Operation",
                "fieldtype": "Data",
                "insert_after": "master_work_order",
                "read_only": 1,
                "print_hide": 1,
            },
        ],
    }
    create_custom_fields(custom_fields, ignore_validate=True)
    frappe.db.commit()