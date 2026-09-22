"""Write the Connection tab again, now that it carries Send to Supplier Qty.

backfill_master_work_order_connections has already run wherever the tab exists, and
a patch runs once, so the stored rows would keep a Send to Supplier Qty of zero
until something else happened to move them.

Only what is stored is at stake: the form reads the tab fresh each time it opens --
see MasterWorkOrder.onload() -- so this is for the report, the list view and the
print format, which reach the rows without it.
"""

from textile_manufacturing.patches import backfill_master_work_order_connections


def execute():
	backfill_master_work_order_connections.execute()
