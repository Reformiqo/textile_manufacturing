"""The goods coming back from the supplier.

The other half of subcontracting driven by a Master Work Order: the supplier was sent
the finished goods of the order to work on, not raw material to build them from, so
what comes back is accounted for against those same goods.

Kept apart from subcontracting_order.py so a hook path says which document fires it.
"""

import frappe
from frappe.utils import flt


def set_receipt_master_work_order(doc, method=None):
    """Carry the Master Work Order in, and rewrite what the supplier consumed.

    The supplier was sent the finished goods to work on, so those are what he
    consumed -- the BOM's raw material never reached him, and listing it here would
    write off stock that never moved."""
    # The Subcontracting Order is linked on the item rows, not on the receipt itself.
    orders = {
        item.get("subcontracting_order")
        for item in (doc.get("items") or [])
        if item.get("subcontracting_order")
    }
    if not orders:
        return

    master_work_order = next(
        (
            mwo
            for mwo in (
                frappe.db.get_value("Subcontracting Order", order, "master_work_order")
                for order in orders
            )
            if mwo
        ),
        None,
    )
    if not master_work_order:
        return

    doc.master_work_order = master_work_order
    doc.supplied_items = []

    for item in doc.get("items"):
        qty = flt(item.qty)
        if qty <= 0:
            continue

        rate = flt(item.get("rate"))
        doc.append("supplied_items", {
            "main_item_code": item.item_code,
            "rm_item_code": item.item_code,
            "item_name": item.get("item_name"),
            "stock_uom": item.get("stock_uom"),
            "conversion_factor": 1.0,
            "required_qty": qty,
            "consumed_qty": qty,
            "rate": rate,
            "amount": qty * rate,
            "reference_name": item.get("name"),
            "subcontracting_order": item.get("subcontracting_order"),
        })


def update_master_work_order_status(doc, method=None):
    """Ask the Master Work Order to settle its status once the goods are back.

    An order with an Out House operation is not finished when the line is -- the cloth
    is away being worked on -- so it is held In Process until the Subcontracting Order
    it went out on is complete.

    It is hooked here, on the receipt, and not on the Subcontracting Order: that order
    is only Open when it is submitted, and it is this receipt that carries it to
    Completed, in set_subcontracting_order_status() inside its own on_submit. Frappe
    runs doc_events after the controller's method, so by the time this is reached the
    order behind it already reads Completed. Nothing else could be hooked instead --
    ERPNext completes it with db_set, which raises no document event at all."""
    master_work_order = doc.get("master_work_order")
    if not master_work_order:
        return

    order = frappe.get_doc("Master Work Order", master_work_order)
    if order.docstatus != 1:
        return
    if order.status in ("Closed", "Stopped", "Cancelled"):
        return

    order.set_status_from_work_orders()
