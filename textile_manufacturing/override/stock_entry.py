import frappe
from frappe.utils import flt

from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry


# def update_master_job_card_transfer(doc, method=None):
#     master_job_card = doc.get("master_job_card")
#     if not master_job_card:
#         return

#     exclude = doc.name if method == "on_cancel" else None
#     recompute_required_item_transfers(master_job_card, exclude_stock_entry=exclude)

#     if method == "on_submit":
#         current = frappe.db.get_value("Master Job Card", master_job_card, "status")
#         if current not in ("Work In Progress", "On Hold", "Completed", "Cancelled"):
#             frappe.db.set_value(
#                 "Master Job Card", master_job_card, "status", "Material Transferred"
#             )


# def recompute_required_item_transfers(master_job_card, exclude_stock_entry=None):
#     mjc = frappe.get_doc("Master Job Card", master_job_card)

#     transferred = {}
#     last_reference = {}

#     # Material Transfer only. The SFG Stock In/Out entries carry the same
#     # master_job_card link, and counting those as a transfer credited the operation
#     # with material it never received -- they move its output, not its input.
#     stock_entries = frappe.get_all(
#         "Stock Entry",
#         filters={
#             "master_job_card": master_job_card,
#             "purpose": "Material Transfer for Manufacture",
#             "docstatus": 1,
#         },
#         pluck="name",
#         order_by="creation",
#     )

#     if exclude_stock_entry and exclude_stock_entry in stock_entries:
#         stock_entries.remove(exclude_stock_entry)

#     if stock_entries:
#         details = frappe.get_all(
#             "Stock Entry Detail",
#             filters={"parent": ["in", stock_entries]},
#             fields=["parent", "item_code", "qty"],
#             order_by="creation",
#         )
#         for d in details:
#             transferred[d.item_code] = transferred.get(d.item_code, 0.0) + flt(d.qty)
#             last_reference[d.item_code] = d.parent

#     for row in mjc.required_item:
#         transfer_qty = transferred.get(row.item_code, 0.0)
#         # Same formula as calculate_required_items(), or the next save of the card
#         # would quietly move the figure this hook just wrote.
#         pending = flt(row.requried_qty) - transfer_qty + flt(row.return_qty)
#         frappe.db.set_value(
#             "Master Job Card Required Item",
#             row.name,
#             {
#                 "transfer_qty": transfer_qty,
#                 "pending_transfer_qty": pending,
#                 "stock_entry_reference": last_reference.get(row.item_code),
#             },
#             update_modified=False,
#         )



def update_master_work_order_returns(doc, method=None):
    if not doc.get("master_work_order") or not doc.is_return:
        return

    entries = frappe.get_all(
        "Stock Entry",
        filters={
            "master_work_order": doc.master_work_order,
            "is_return": 1,
            "docstatus": 1,
        },
        pluck="name",
    )

    returned_map = {}
    if entries:
        returned_by_item = frappe.db.sql(
            """
            SELECT sed.item_code, SUM(sed.qty) as qty
            FROM `tabStock Entry Detail` sed
            WHERE sed.parent IN %(entries)s
            GROUP BY sed.item_code
            """,
            {"entries": entries},
            as_dict=True,
        )
        returned_map = {r.item_code: flt(r.qty) for r in returned_by_item}

    required_item_rows = frappe.get_all(
        "Master Work Order Required Item",
        filters={"parent": doc.master_work_order},
        fields=["name", "item_code", "return_qty"],
    )

    for item in required_item_rows:
        new_returned = returned_map.get(item.item_code, 0)

        if flt(item.return_qty) != new_returned:
            frappe.db.set_value("Master Work Order Required Item", item.name, "return_qty", new_returned)


def update_master_work_order_consumed(doc, method=None):
    if doc.purpose != "Manufacture" or not doc.work_order:
        return

    entries = frappe.get_all(
        "Stock Entry",
        filters={
            "work_order": doc.work_order,
            "purpose": "Manufacture",
            "docstatus": 1,
        },
        pluck="name",
    )

    consumed_map = {}
    if entries:
        consumed_by_item = frappe.db.sql(
            """
            SELECT item_code, SUM(qty) as qty
            FROM `tabStock Entry Detail`
            WHERE parent IN %(entries)s
            AND s_warehouse IS NOT NULL
            AND t_warehouse IS NULL
            GROUP BY item_code
            """,
            {"entries": entries},
            as_dict=True,
        )
        consumed_map = {r.item_code: flt(r.qty) for r in consumed_by_item}

    required_item_rows = frappe.get_all(
        "Master Work Order Required Item",
        filters={"parent": doc.master_work_order},
        fields=["name", "item_code", "consumed_qty"],
    )

    for item in required_item_rows:
        new_consumed = consumed_map.get(item.item_code, 0)
        if flt(item.consumed_qty) != new_consumed:
            frappe.db.set_value("Master Work Order Required Item", item.name, "consumed_qty", new_consumed)

class CustomStockEntry(StockEntry):
    def validate_subcontract_order(self):
        """ERPNext's Raw Materials Supplied check does not apply to this app's transfers.

        It insists every item on a Send to Subcontractor entry appears in the
        Subcontracting Order's Raw Materials Supplied table. That table is emptied on
        purpose here -- the supplier is sent the finished goods to work on, not raw
        material to build them from -- so the check would refuse every transfer raised
        against a Master Work Order.

        Skipping it is what lets the entry carry its Subcontracting Order at all, and
        that link is what the Connections panel follows and what
        update_subcontracting_order_status() reads to move the order on.

        Anything not driven by a Master Work Order is ordinary subcontracting, with a
        Raw Materials Supplied table of its own, and is checked exactly as before."""
        if self.get("master_work_order"):
            return

        super().validate_subcontract_order()
