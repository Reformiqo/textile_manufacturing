"""Fill the Connection tab of every Master Work Order raised before it existed.

The tab is read again each time a form opens -- see MasterWorkOrder.onload() -- so
this is not what makes it readable. It is what puts the rows in the database, where
a report, a list view or a print format can reach them without the form.
"""

import frappe


def execute():
	for name in frappe.get_all(
		"Master Work Order", filters={"docstatus": 1}, pluck="name", order_by="creation"
	):
		try:
			frappe.get_doc("Master Work Order", name).update_connections()
			frappe.db.commit()
		except Exception:
			# One order that cannot be read is not a migration that should stop:
			# the rest of them are still worth writing, and the tab draws itself
			# from scratch when the form is opened either way.
			frappe.db.rollback()
			frappe.log_error(
				title="Master Work Order Connection tab",
				message=f"{name}\n\n{frappe.get_traceback()}",
			)
