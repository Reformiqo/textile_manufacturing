"""Subcontracting driven by a Master Work Order.

Out House work here is priced per operation, not bought against a supplier BOM: the
supplier is sent the goods themselves to work on, not the raw material to build them
from. So the transfer out carries the finished goods of the order rather than the
BOM's Supplied Items.
"""

import frappe
from frappe.utils import flt


def set_master_work_order(doc, method=None):
    """Carry the Master Work Order down from the Purchase Order, and drop the
    Supplied Items it renders meaningless.

    The supplier is being paid for an operation on goods that are sent to him, not to
    build the item from raw material, so there is nothing to supply."""
    if not doc.purchase_order:
        return

    doc.master_work_order = frappe.db.get_value(
        "Purchase Order", doc.purchase_order, "master_work_order"
    )

    if doc.master_work_order:
        doc.supplied_items = []


def master_work_order_of(subcontract_order, order_doctype="Subcontracting Order"):
    """The Master Work Order behind this order, if there is one."""
    if not subcontract_order:
        return None

    if order_doctype == "Purchase Order":
        return frappe.db.get_value("Purchase Order", subcontract_order, "master_work_order")

    return frappe.db.get_value(
        "Subcontracting Order", subcontract_order, "master_work_order"
    )


@frappe.whitelist()
def make_rm_stock_entry(
    subcontract_order, rm_items=None, order_doctype="Subcontracting Order", target_doc=None
):
    """Send the goods out, falling back to ERPNext for ordinary subcontracting."""
    from erpnext.controllers.subcontracting_controller import (
        make_rm_stock_entry as erpnext_make_rm_stock_entry,
    )

    master_work_order = master_work_order_of(subcontract_order, order_doctype)
    if not master_work_order:
        return erpnext_make_rm_stock_entry(
            subcontract_order, rm_items, order_doctype, target_doc
        )

    return make_finished_goods_stock_entry(
        subcontract_order, order_doctype, master_work_order
    )


def make_finished_goods_stock_entry(subcontract_order, order_doctype, master_work_order):
    order = frappe.get_doc(order_doctype, subcontract_order)

    if not order.get("supplier_warehouse"):
        frappe.throw(
            ("Supplier Warehouse is required before the goods can be sent out."),
            title="Supplier Warehouse Missing",
        )

    stock_entry = frappe.new_doc("Stock Entry")
    stock_entry.stock_entry_type = "Send to Subcontractor"
    stock_entry.purpose = "Send to Subcontractor"
    stock_entry.company = order.company
    stock_entry.supplier = order.supplier
    stock_entry.supplier_warehouse = order.supplier_warehouse
    stock_entry.to_warehouse = order.supplier_warehouse
    stock_entry.master_work_order = master_work_order

    # The order this transfer was raised against. ERPNext sets it in the post_process
    # of its own make_rm_stock_entry, and everything downstream reads it: the
    # Subcontracting Order's Connections list the Stock Entry through this field, and
    # update_subcontracting_order_status() moves the order on to Material Transferred.
    #
    # It also switches on validate_subcontract_order(), which insists every item
    # transferred appears in the order's Raw Materials Supplied table -- empty here by
    # design. CustomStockEntry skips that one check for a Master Work Order transfer.
    if order_doctype == "Purchase Order":
        stock_entry.purchase_order = order.name
    else:
        stock_entry.subcontracting_order = order.name

    for item in order.get("items"):
        qty = flt(item.qty)
        if qty <= 0:
            continue

        stock_entry.append("items", {
            "item_code": item.item_code,
            "item_name": item.get("item_name"),
            "qty": qty,
            "uom": item.get("stock_uom"),
            "stock_uom": item.get("stock_uom"),
            "conversion_factor": 1.0,
            "s_warehouse": item.get("warehouse"),
            "t_warehouse": order.supplier_warehouse,
        })

    if not stock_entry.get("items"):
        frappe.throw(("There is nothing to send out."), title="Nothing to Transfer")

    return stock_entry
