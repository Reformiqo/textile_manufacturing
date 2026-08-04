import frappe
from erpnext.manufacturing.doctype.work_order.work_order import WorkOrder

class CustomWorkOrder(WorkOrder):
    # we have to bypass this operations sequence related logic
    def validate_operations_sequence(self):
        pass



def on_update(doc, method=None):
    if doc.status != "Completed":
        return

    # Find the Master Work Order this Work Order was created from.
    parent = frappe.db.get_value(
        "Master Work Order Item", {"work_order_number": doc.name}, "parent"
    )
    if not parent:
        return

    # Collect the Work Orders of every sibling row on the same Master Work Order.
    sibling_work_orders = frappe.get_all(
        "Master Work Order Item",
        filters={"parent": parent, "parenttype": "Master Work Order"},
        pluck="work_order_number",
    )
    sibling_work_orders = [wo for wo in sibling_work_orders if wo]
    if not sibling_work_orders:
        return

    statuses = frappe.get_all(
        "Work Order",
        filters={"name": ["in", sibling_work_orders]},
        pluck="status",
    )

    # Only update the Master Work Order once this is the last Work Order to finish.
    if statuses and all(status == "Completed" for status in statuses):
        frappe.db.set_value(
            "Master Work Order",
            parent,
            {
                "actual_end_date": frappe.utils.now_datetime(),
                "status": "Completed",
            },
        )
