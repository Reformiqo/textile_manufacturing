"""Name the operation on the subcontracting documents already raised.

The row name is hidden now -- see custom_fields.py -- so a Purchase Order raised
before the Operation field existed would show nothing at all where the operation
should be. Each of them already says which operation row it was raised for, and
the row says which operation, so the name is there to be read off.

The fields are made here rather than waited for: custom fields are created in
after_migrate, which runs after the patches do.
"""

import frappe

from textile_manufacturing.custom_fields import make_custom_fields

NAME_FIELD = "master_work_order_operation_name"
ROW_FIELD = "master_work_order_operation"


def execute():
	make_custom_fields()

	for doctype in ("Purchase Order", "Subcontracting Order"):
		name_from_operation_row(doctype)

	# Last, so the orders it reads have been named already.
	name_receipts_from_their_orders()


def operation_name(row, cache):
	"""The operation a Master Work Order Operation row runs.

	Read one row at a time through frappe.db: a child table cannot be queried
	without its parent doctype, and the row is all these documents carry."""
	if row not in cache:
		cache[row] = frappe.db.get_value("Master Work Order Operation", row, "opration_name")

	return cache[row]


def name_from_operation_row(doctype):
	documents = frappe.get_all(
		doctype,
		filters={ROW_FIELD: ["is", "set"], NAME_FIELD: ["is", "not set"]},
		fields=["name", ROW_FIELD],
	)
	if not documents:
		return

	cache = {}
	for document in documents:
		named = operation_name(document.get(ROW_FIELD), cache)
		if not named:
			# The line was taken off the order after the work went out. Nothing
			# left to read the name off, and the row name still says which.
			continue

		frappe.db.set_value(doctype, document.name, NAME_FIELD, named, update_modified=False)

	frappe.db.commit()


def name_receipts_from_their_orders():
	"""A receipt takes its operation off the orders its rows came back on.

	The same rule set_receipt_master_work_order() goes by, and one covering two
	operations names neither -- there is no one answer."""
	receipts = frappe.get_all(
		"Subcontracting Receipt",
		filters={"master_work_order": ["is", "set"], NAME_FIELD: ["is", "not set"]},
		pluck="name",
	)
	if not receipts:
		return

	for receipt in receipts:
		orders = {
			row.subcontracting_order
			for row in frappe.get_all(
				"Subcontracting Receipt Item",
				filters={"parent": receipt, "parenttype": "Subcontracting Receipt"},
				fields=["subcontracting_order"],
			)
			if row.subcontracting_order
		}
		if not orders:
			continue

		values = {}
		for fieldname in (ROW_FIELD, NAME_FIELD):
			named = {frappe.db.get_value("Subcontracting Order", order, fieldname) for order in orders}
			named.discard(None)
			named.discard("")
			if len(named) == 1:
				values[fieldname] = named.pop()

		if values:
			frappe.db.set_value("Subcontracting Receipt", receipt, values, update_modified=False)

	frappe.db.commit()
