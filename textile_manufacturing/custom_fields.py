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
                "hidden": 1,
                "print_hide": 1,
            },
            {
                # Carried down from the Purchase Order -- see its field of the
                # same name.
                "fieldname": "master_work_order_operation_name",
                "label": "Operation",
                "fieldtype": "Link",
                "options": "Operation",
                "insert_after": "master_work_order_operation",
                "read_only": 1,
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
            },
            {
                # The last leg of the trail names its operation too, so the goods
                # coming back say what was done to them without the reader having
                # to open the order they went out on.
                "fieldname": "master_work_order_operation",
                "label": "Master Work Order Operation",
                "fieldtype": "Data",
                "insert_after": "master_work_order",
                "read_only": 1,
                "hidden": 1,
                "print_hide": 1,
            },
            {
                "fieldname": "master_work_order_operation_name",
                "label": "Operation",
                "fieldtype": "Link",
                "options": "Operation",
                "insert_after": "master_work_order_operation",
                "read_only": 1,
            },
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
                #
                # Hidden: it is a row name, and a row name is linkage rather than
                # information. Everything downstream is keyed on it, so it stays on
                # the document -- but what a reader wants where the operation should
                # be is Cutwork, not 6lin00j9fo, and that is the field below.
                "fieldname": "master_work_order_operation",
                "label": "Master Work Order Operation",
                "fieldtype": "Data",
                "insert_after": "master_work_order",
                "read_only": 1,
                "hidden": 1,
                "print_hide": 1,
            },
            {
                # The operation by name, beside the row it points at. The row name
                # says which line, and says nothing to anybody reading the order:
                # this is what puts Cutwork on the face of it, and on the print,
                # so the supplier and the buyer are looking at the same job work.
                "fieldname": "master_work_order_operation_name",
                "label": "Operation",
                "fieldtype": "Link",
                "options": "Operation",
                "insert_after": "master_work_order_operation",
                "read_only": 1,
            },
        ],
    }
    create_custom_fields(custom_fields, ignore_validate=True)
    frappe.db.commit()