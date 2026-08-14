import frappe
from erpnext.manufacturing.doctype.production_plan.production_plan import ProductionPlan


class CustomProductionPlan(ProductionPlan):
    """A finished Master Work Order finishes the Production Plan behind it.

    Left to itself the plan never gets there on this app's orders. all_items_completed()
    waits for produced to have caught planned, and produced counts the good pieces
    only, so an order that destroyed any never satisfies it however finished it is --
    10 planned and 8 made reads as 2 still to come. Short of Completed, set_status()
    goes on to update_requested_status(), which reads Material Requested off any
    Material Request Plan Item still carrying a requested qty. So a plan whose order
    has nothing left to run sits in Material Requested for good.

    Nothing about the Material Request flow changes. A plan still says Material
    Requested for as long as there is work left in it, and ERPNext ranks the two the
    same way itself: set_status() only asks update_requested_status() when it has not
    already reached Completed. All that moves is when Completed is reached.

    Answered here rather than in Master Work Order.update_production_plan() because
    the status is worked out again from four places, and each of them would undo a
    status written over the top: the plan's own validate(), update_produced_pending_qty(),
    the Close and Re-open buttons, and Material Request.update_requested_qty_in_production_plan(),
    which recomputes on every Material Request submitted or cancelled against the plan.
    """

    def all_items_completed(self):
        """Also completed once the Master Work Order raised against the plan is.

        Answered where ERPNext asks the question, so that set_status() arrives at
        Completed down its own road and everything hanging off the status follows
        without being corrected afterwards. The reserved qty above all: set_status()
        leaves the bin alone for a Completed plan and
        get_reserved_qty_for_production_plan() passes over one, which agree only if
        the status is settled before that decision is taken.

        Only the status is ours to say. The quantities are left as
        update_production_plan() wrote them, so the plan goes on reporting what was
        really made rather than what was put through the line."""
        if super().all_items_completed():
            return True

        return bool(self.completed_master_work_order())

    def completed_master_work_order(self):
        """The finished Master Work Order raised against this plan, if there is one.

        Only ever one is live -- Master Work Order.validate_unique_production_plan()
        sees to that -- so a Completed one leaves nothing on the plan still to run.
        Whether the Out House work has come back from the supplier is already inside
        that status; see Master Work Order.set_status_from_work_orders()."""
        if not self.name:
            return None

        return frappe.db.exists(
            "Master Work Order",
            {
                "production_plan_number": self.name,
                "docstatus": 1,
                "status": "Completed",
            },
        )
