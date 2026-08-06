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
