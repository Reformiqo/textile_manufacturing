"""Stamp Has Master Job Card on the operations already running.

The flag is what holds Manufacturing Type still on the form, and it is written when
the card is raised -- so without this the orders that were already live when it was
introduced would go on offering their operations for re-routing.
"""

import frappe


def execute():
    frappe.db.sql(
        """
        update `tabMaster Work Order Operation` op
        join `tabMaster Job Card` card
            on card.master_work_order_number = op.parent
            and card.operation_name = op.opration_name
            and card.docstatus < 2
        set op.has_master_job_card = 1
        where op.parenttype = 'Master Work Order'
        """
    )
