"""Purchase Orders raised from a Master Work Order.

Out House work is priced per operation on goods that are sent to the supplier, so a
row buys one unit of the operation for each unit made. Ordinary subcontracting is
left alone -- there the service line and the finished goods are genuinely different
quantities, and the ratio between them is the point.
"""

import frappe
from frappe.utils import flt


def keep_fg_qty_in_step(doc, method=None):
    """Hold the finished goods qty to the qty ordered.

    ERPNext sizes the Subcontracting Order by dividing the two figures on the row --
    conversion_factor = qty / fg_item_qty -- and then raises the order for
    available qty / conversion factor. The two are written out together, but only qty
    is on the items grid, so cutting a row from 10 to 1 left fg_item_qty at 10: a
    conversion factor of 0.1, and a Subcontracting Order back at 10 for the finished
    goods, the one figure the change was meant to bring down.

    Kept in step the factor is 1, and the order goes out for exactly what the
    Purchase Order says.
    """
    if not doc.get("master_work_order"):
        return

    for row in (doc.get("items") or []):
        if flt(row.get("fg_item_qty")) != flt(row.qty):
            row.fg_item_qty = flt(row.qty)


def link_operation_to_purchase_order(doc, method=None):
    """Write this Purchase Order onto the operation line it was raised for.

    The line already carries a Subcontracting PO Number and nothing ever filled it
    in. Filled in here, the trail runs Master Work Order -> operation line -> the
    items it selected -> the Purchase Order that sent them out, and the planner can
    see from the operations table which of them is away and on what.

    Cleared again on cancel: the order that sent the cloth out no longer stands, and
    a line pointing at a cancelled one reads as still away."""
    operation = doc.get("master_work_order_operation")
    if not doc.get("master_work_order") or not operation:
        return

    # Only ever this order's own line. A stray value is not allowed to stamp another
    # Master Work Order's operations.
    parent = frappe.db.get_value("Master Work Order Operation", operation, "parent")
    if parent != doc.master_work_order:
        return

    frappe.db.set_value(
        "Master Work Order Operation",
        operation,
        "subcontracting_po_number",
        doc.name if doc.docstatus == 1 else None,
        update_modified=False,
    )
