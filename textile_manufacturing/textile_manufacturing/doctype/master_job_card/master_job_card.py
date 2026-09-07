# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import flt, now_datetime, time_diff_in_seconds


# A Master Work Order Operation row carries a coarser status than the card does.
OPERATION_STATUS = {
    "Draft": "Pending",
    "Open": "Pending",
    "Cancelled": "Pending",
    "Material Transferred": "Work In Progress",
    "Work In Progress": "Work In Progress",
    "On Hold": "Work In Progress",
    "Completed": "Completed",
}

# Out of the operation and into store is a receipt; back onto the floor to be
# worked is a consumption.
SFG_STOCK_ENTRY_TYPE = {
    "Stock Out": "Material Receipt",
    "Stock In": "Material Consumption for Manufacture",
}

ACCOUNTED_FIELDS = ("completed_qty", "process_loss_qty", "rejected_qty")

def accounted_qty(source):
    return sum(flt(source.get(field)) for field in ACCOUNTED_FIELDS)


def operation_status(card_statuses):
    """One status for an operation running on several Master Job Cards.

    Done only when every card is; started as soon as any one of them is."""
    mapped = [OPERATION_STATUS.get(status, "Pending") for status in card_statuses]
    if not mapped:
        # Every card cancelled -- the operation is back to not having been run.
        return "Pending"

    if all(status == "Completed" for status in mapped):
        return "Completed"
    if any(status != "Pending" for status in mapped):
        return "Work In Progress"

    return "Pending"


class MasterJobCard(Document):
    def onload(self):
        self.set_onload("qty_caps", self.qty_caps())

    def validate(self):
        self.validate_quality_inspection()
        self.validate_rejection_reason()

    def before_save(self):
        self.recalculate()

    def before_update_after_submit(self):
        self.recalculate()

    def recalculate(self):
        self.recalculate_time_logs()
        self.calculate_detail_rows()
        self.calculate_scrap_items()
        self.calculate_sfg_stock()
        self.calculate_totals()

    def after_insert(self):
        self.link_job_cards()
        self.set_card_status("Open")

    def on_update(self):
        self.sync_employees_to_job_cards()
        self.sync_job_card_quantities()
        self.sync_scrap_to_master_work_order()
        self.sync_to_master_work_order()

    def scrap_rows_changed(self):
        """Did this save touch the scrap table at all?"""
        def snapshot(doc):
            return sorted(
                (row.item_code or "", flt(row.scrap_qty), row.uom or "",
                 row.scrap_warehouse or "")
                for row in (doc.get("scrap_item") or [])
            )

        before = self.get_doc_before_save()
        if not before:
            return bool(self.get("scrap_item"))

        return snapshot(self) != snapshot(before)

    def sync_scrap_to_master_work_order(self, force=False):
        """Roll every card's scrap up onto the order, one row per scrap item.

        force skips the did-the-table-change gate, for the cancel: the rows are
        exactly as they were, and it is the card being withdrawn that moves the
        total."""
        if not self.master_work_order_number:
            return
        if not force and not self.scrap_rows_changed():
            return

        cards = frappe.get_all(
            "Master Job Card",
            filters={
                "master_work_order_number": self.master_work_order_number,
                "docstatus": ["<", 2],
                "name": ["!=", self.name],
            },
            pluck="name",
        )

        # A cancelled card scrapped nothing in the end -- the same reckoning
        # operation_totals() makes of its quantities, and for the same reason: the
        # work went back with the Job Cards, and so did what it spoiled. It is left
        # out here rather than filtered out of the query below because the query
        # already excludes it by name; this is the half that adds it back in.
        rows = [] if self.docstatus == 2 else [
            row.as_dict() for row in (self.get("scrap_item") or [])
        ]
        if cards:
            rows += frappe.get_all(
                "Master Job Card Scrap Item",
                filters={"parent": ["in", cards], "parenttype": "Master Job Card"},
                fields=["item_code", "item_name", "uom", "scrap_qty", "scrap_warehouse"],
            )

        grouped = {}
        for row in rows:
            if not row.get("item_code"):
                continue

            entry = grouped.setdefault(row["item_code"], {
                "item_code": row["item_code"],
                "item_name": row.get("item_name"),
                "uom": row.get("uom"),
                "scrap_warehouse": row.get("scrap_warehouse"),
                "scrap_qty": 0.0,
            })
            entry["scrap_qty"] += flt(row.get("scrap_qty"))

        self.write_master_work_order_scrap(grouped)
        self.write_master_work_order_item_scrap(
            flt(sum(entry["scrap_qty"] for entry in grouped.values()), 3)
        )

    def write_master_work_order_item_scrap(self, total):
        """Total scrap of the order, onto every Item to be Manufacture row.

        A scrap row names no manufactured item, so there is nothing to split it by."""
        for row in frappe.get_all(
            "Master Work Order Item",
            filters={
                "parent": self.master_work_order_number,
                "parenttype": "Master Work Order",
            },
            fields=["name", "scrap_qty"],
        ):
            if flt(row.scrap_qty) == total:
                continue

            frappe.db.set_value(
                "Master Work Order Item", row.name, "scrap_qty", total,
                update_modified=False,
            )

    def write_master_work_order_scrap(self, grouped):
        existing = {
            row.item_code: row
            for row in frappe.get_all(
                "Master Work Order Scrap Item",
                filters={
                    "parent": self.master_work_order_number,
                    "parenttype": "Master Work Order",
                },
                fields=["name", "idx", "item_code", "item_name", "uom",
                        "scrap_qty", "scrap_warehouse"],
            )
        }

        for idx, entry in enumerate(grouped.values(), start=1):
            row = existing.pop(entry["item_code"], None)
            if not row:
                self.insert_master_work_order_scrap(entry, idx)
                continue

            values = dict(entry, idx=idx)
            changed = {
                field: value for field, value in values.items() if row.get(field) != value
            }
            if changed:
                frappe.db.set_value(
                    "Master Work Order Scrap Item", row.name, changed, update_modified=False
                )

        # Only the rows for an item that is no longer scrapped anywhere.
        if existing:
            frappe.db.delete("Master Work Order Scrap Item", {
                "name": ["in", [row.name for row in existing.values()]],
            })

    def insert_master_work_order_scrap(self, entry, idx):
        row = frappe.new_doc("Master Work Order Scrap Item")
        row.update(entry)
        row.parent = self.master_work_order_number
        row.parenttype = "Master Work Order"
        row.parentfield = "scrap_item"
        row.idx = idx
        row.docstatus = frappe.db.get_value(
            "Master Work Order", self.master_work_order_number, "docstatus"
        )
        row.db_insert()

    def before_submit(self):
        self.submit_completed_job_cards()

    def on_submit(self):
        self.validate_jobs_completed()
        self.set_card_status("Completed")

    def before_cancel(self):
        self.validate_cancel()
        self.release_back_links()

    def on_trash(self):
        """Let go of everything on the way out, the same as a cancel does.

        A card that was never submitted is never cancelled -- there is nothing to
        cancel -- so it is deleted instead, and the releasing that on_cancel does had
        no counterpart on that road. The two ends then held each other and neither
        could go: the card cannot be deleted while its Job Cards carry its name, and
        the Job Cards cannot be deleted while the card's detail rows name them.
        "Cannot delete or cancel because Job Card X is linked with Master Job Card Y",
        whichever of the two was reached for.

        Run here rather than in a before_delete, because on_trash is called before
        Frappe checks the links (delete_doc) -- so by the time it looks, there is
        nothing left pointing this way.

        The Job Cards go with it rather than being left behind. A Master Job Card is
        the only thing that drives them here -- nothing else starts, pauses or
        completes one -- so a Job Card whose card has been deleted is work nobody can
        report against, cluttering the Work Order and waiting to be claimed by the
        next card raised for the operation. Deleted, the Work Order is back where it
        was before this card was raised, and link_job_cards() raises a fresh one for
        whatever comes next.

        Idempotent, because this runs on the second road as well: a cancelled card is
        often deleted afterwards, and by then release_job_cards() has already let go
        of everything. The writes below are then no-ops and the loop has nothing left
        to delete."""
        self.release_back_links()
        self.delete_job_cards()

    def delete_job_cards(self):
        """Take the Job Cards with the card that held them.

        The rows are unnamed first. This card's Master Job Card Detail and Time Log
        rows still name the Job Cards while on_trash runs -- they are deleted with the
        parent a moment later, but Frappe checks the links before that and would
        refuse: "Cannot delete or cancel because Job Card X is linked with Master Job
        Card Y". Unnaming them costs nothing, since the rows themselves are about to
        go.

        What was raised against a Job Card goes first, for the same reason and with
        the same refusal behind it. An inspection can be taken on a draft card and a
        transfer posted against its Job Card, and Frappe will not delete a document
        either of those still points at -- "linked with Quality Inspection ...". They
        are cancelled here exactly as release_job_cards() cancels them on the other
        road, so the two ends of the card's life let go of the same things.

        A submitted Job Card is left alone and merely released. Frappe will not delete
        one, and it is not this card's to cancel on a delete -- a card being deleted
        was never submitted, so a submitted Job Card under it is work somebody
        reported by hand and is theirs to deal with."""
        for doctype in ("Master Job Card Detail", "Master Job Card Time Log"):
            frappe.db.set_value(
                doctype,
                {"parent": self.name, "parenttype": "Master Job Card"},
                "job_card_number",
                None,
                update_modified=False,
            )

        for name in self.linked_job_cards():
            frappe.db.set_value(
                "Job Card", name, "master_job_card", None, update_modified=False
            )

            if frappe.db.get_value("Job Card", name, "docstatus") == 1:
                continue

            self.cancel_job_card_stock_entries(name)
            self.cancel_job_card_inspections(name)

            frappe.delete_doc(
                "Job Card", name, ignore_permissions=True, ignore_missing=True
            )

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------
    #
    # A Master Work Order wraps the Work Orders and a Master Job Card wraps the Job
    # Cards, so the rules for letting one go are ERPNext's own rules, read one level
    # up. ERPNext refuses on two counts, and both are answered here:
    #
    #   Work Order.validate_cancel()          -- a Stopped order cannot be cancelled,
    #                                            and neither can one with submitted
    #                                            Stock Entries standing against it.
    #   Job Card.validate_produced_quantity() -- a card cannot be cancelled while the
    #                                            Work Order has produced more than its
    #                                            operations account for; cancel the
    #                                            Manufacturing Entries first.
    #
    # Both are asked up front, in validate_cancel(), so the refusal lands before a
    # single Job Card or Stock Entry has been touched. ERPNext's own second check
    # still runs underneath, from inside job_card.cancel(), and cancel_job_card()
    # catches it -- it works per operation where this one works per order, so it is
    # the finer of the two and is left to have the last word.
    #
    # What the cancel then does, in order:
    #
    #   before_cancel  validate_cancel()        may refuse; nothing touched yet
    #                  release_back_links()     this card's name off the cards after it
    #   on_cancel      release_job_cards()      the Job Cards, and the material they drew
    #                  cancel_own_stock_entries()  the semi-finished goods it posted
    #                  set_card_status()        and the operation's figures with it

    # Shut by hand, and not by anything the floor did. Cancelled is not among them:
    # a card is cancelled before its order can be, so refusing here would leave the
    # pair with no way out of each other. Completed is not among them either -- the
    # Finish is what makes it, and the Finish is answered by its own postings, which
    # let go the moment they are cancelled. A status would not.
    SHUT_ORDER_STATUS = ("Closed", "Stopped")

    def validate_cancel(self):
        """Refuse the cancel while the order is shut, or has been produced against.

        The Finish books the production on the Master Work Order, not on this card,
        so an operation that has been produced against cannot simply be handed back
        -- the order would be left reporting goods that no operation accounts for.

        Read off the Manufacture entries rather than off the order's status, for the
        same reason ERPNext reads it off the Work Order's produced qty: the status
        says the order was finished once, the entries say the goods are still booked.
        Cancel the Finish and the refusal lifts with it, which is exactly what the
        message asks for -- a refusal that named a step that did not work would be
        worse than no message at all."""
        if not self.master_work_order_number:
            return
        if not frappe.db.exists("Master Work Order", self.master_work_order_number):
            return

        status = frappe.db.get_value(
            "Master Work Order", self.master_work_order_number, "status"
        )
        if status in self.SHUT_ORDER_STATUS:
            frappe.throw(
                ("Master Work Order {0} is {1}, so the work under it cannot be "
                 "cancelled.<br><br>{2}").format(
                    frappe.utils.get_link_to_form(
                        "Master Work Order", self.master_work_order_number
                    ),
                    frappe.bold(status),
                    ("Re-open the order first." if status == "Stopped" else
                     "A Closed order cannot be re-opened, so the work under it "
                     "stands as it was run."),
                ),
                title="Master Work Order {0}".format(status),
            )

        entries = self.manufacture_entries()
        if not entries:
            return

        frappe.throw(
            ("{0} has been produced against, so {1} cannot be cancelled -- the "
             "order would be left reporting goods that no operation accounts "
             "for.<br><br>"
             "Cancel the Manufacture Stock Entry(s) raised by the Finish on {2} "
             "first, then cancel this card:<br>{3}").format(
                frappe.bold(self.master_work_order_number),
                frappe.bold(self.name),
                frappe.utils.get_link_to_form(
                    "Master Work Order", self.master_work_order_number
                ),
                "<br>".join(
                    frappe.utils.get_link_to_form("Stock Entry", entry)
                    for entry in entries
                ),
            ),
            title="Production Booked Against This Order",
        )

    def manufacture_entries(self):
        """The Finish's postings. They carry the order's name, never this card's."""
        return frappe.get_all(
            "Stock Entry",
            filters={
                "master_work_order": self.master_work_order_number,
                "purpose": "Manufacture",
                "docstatus": 1,
            },
            pluck="name",
        )

    def on_cancel(self):
        # The Job Cards first, because they are the only thing left that can refuse:
        # validate_cancel() has answered the order, but ERPNext still asks its own
        # question per operation. Ordered this way that refusal lands before this
        # card's own postings have been touched, so a card that cannot be cancelled
        # keeps its semi-finished goods where they are.
        cancelled = self.release_job_cards()
        cancelled += self.cancel_own_stock_entries()
        self.report_cancelled_stock_entries(cancelled)
        # Completed, Process Loss and Pending come off the order through here:
        # set_card_status() ends in sync_to_master_work_order(), and every figure it
        # writes is worked out from the cards that are still live -- this one is not,
        # so it drops out of all of them. Scrap is the one that does not travel that
        # road, since it is rolled up on save and a cancel is not one.
        self.set_card_status("Cancelled")
        self.sync_scrap_to_master_work_order(force=True)

    def cancel_own_stock_entries(self):
        """Cancel the Stock Entries raised through this card, and only those.

        The ones carrying its name -- the semi-finished goods it posted itself. The
        Finish's Manufacture entries carry the order's name, not this card's, and are
        left alone: they belong to the Master Work Order and are cancelled from it.

        In on_cancel and not before_cancel: this card's sfg_stock rows point at these
        entries, and Frappe will not cancel a document a live one links to -- so they
        only let go once this card is itself cancelled, which it is by the time this
        runs.

        Newest first, since a later entry drew on what an earlier one put into
        store."""
        return self.cancel_stock_entries({"master_job_card": self.name, "docstatus": 1})

    def cancel_stock_entries(self, filters):
        """Cancel the submitted Stock Entries matching filters, newest first.

        Newest first because a later entry drew on what an earlier one moved --
        cancelling the earlier one first would leave the stock negative in between."""
        entries = frappe.get_all(
            "Stock Entry", filters=filters, pluck="name", order_by="creation desc"
        )

        for name in entries:
            frappe.get_doc("Stock Entry", name).cancel()

        return entries

    def report_cancelled_stock_entries(self, entries):
        if not entries:
            return

        frappe.msgprint(
            ("Cancelled {0} Stock Entry(s) raised through this card:<br><br>{1}").format(
                len(entries),
                "<br>".join(
                    frappe.utils.get_link_to_form("Stock Entry", name) for name in entries
                ),
            ),
            title="Stock Entries Cancelled",
            indicator="orange",
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def validate_rejection_reason(self):
        rejected_by_job_card = {}
        for log in self.time_log:
            if not log.job_card_number:
                continue
            rejected_by_job_card[log.job_card_number] = (
                rejected_by_job_card.get(log.job_card_number, 0.0) + flt(log.rejected_qty)
            )

        missing = []
        for row in (self.get("job_card_detail") or []):
            rejected = max(
                flt(rejected_by_job_card.get(row.job_card_number)),
                flt(row.rejected_qty),
            )
            if rejected > 0 and not (row.rejection_reason or "").strip():
                missing.append((row, rejected))

        if not missing:
            return

        frappe.throw(
            ("Rejection Reason is required wherever a Rejected Qty is entered:"
             "<br><br>{0}").format(
                "<br>".join(
                    "Row {0}: {1} -- {2} rejected".format(
                        row.idx, row.item_code or "", flt(rejected, 3)
                    )
                    for row, rejected in missing
                )
            ),
            title="Rejection Reason Missing",
        )

    def other_cards(self):
        return frappe.get_all(
            "Master Job Card",
            filters={
                "master_work_order_number": self.master_work_order_number,
                "docstatus": ["<", 2],
                "name": ["!=", self.name],
            },
            pluck="name",
        )

    def own_operation_loss(self):
        """This card's own loss, but only once Process Loss Qty counts it."""
        if not (self.docstatus == 1 and self.status == "Completed"):
            return {}

        lost = {}
        for row in (self.get("job_card_detail") or []):
            if not row.work_order_number:
                continue
            lost[row.work_order_number] = lost.get(row.work_order_number, 0.0) + (
                flt(row.process_loss_qty) + flt(row.rejected_qty)
            )

        return lost

    def qty_caps(self):
        """Most each row may be raised for:

            order qty - what the other operations lost - what has been finished

        Lost, because the cloth is gone. Finished, because those pieces have been
        booked off the line as goods and are no longer work anybody can take -- a card
        raised for them would be claiming pieces that have already left.

        A card's own loss must not shrink its own quantity, so it is taken back out
        where the field already counts it."""
        if not self.master_work_order_number:
            return {}

        own = self.own_operation_loss()

        caps = {}
        for row in frappe.get_all(
            "Master Work Order Item",
            filters={
                "parent": self.master_work_order_number,
                "parenttype": "Master Work Order",
            },
            fields=[
                "work_order_number",
                "qty_to_manufacture",
                "process_loss_qty",
                "manufacture_qty",
            ],
        ):
            if not row.work_order_number:
                continue

            elsewhere = flt(row.process_loss_qty) - flt(own.get(row.work_order_number))
            caps[row.work_order_number] = max(
                flt(row.qty_to_manufacture) - elsewhere - flt(row.manufacture_qty), 0.0
            )

        return caps

    def sync_job_card_quantities(self):
        for row in (self.get("job_card_detail") or []):
            if row.job_card_number:
                self._sync_job_card_quantity(row, flt(row.qty_to_manufacture))

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def set_card_status(self, status):
        self.db_set("status", status)

        if status == "Completed":
            self.db_set("actual_end_date", now_datetime())
            # The next operation's Qty to Manufacture is entered by hand, so it is not
            # brought down from here -- that would overwrite what the operator typed.
            # What this operation turned out still limits the one after it, but as a
            # ceiling on the pending qty rather than a rewrite of the card:
            # MasterWorkOrder.pending_by_operation() applies it.
            # self.push_ceiling_to_next_operation()

        # Only where the operation's figures actually move. A Start or a Pause reports
        # no quantity, and the draft saves those actions make reach the Master Work
        # Order through on_update anyway.
        #
        # Cancelled belongs here as much as Completed: frappe runs on_cancel instead of
        # on_update, so nothing else would carry the release back, and the operation
        # would keep counting quantity that has been handed back with the Job Cards.
        if status in ("Completed", "Cancelled"):
            self.sync_to_master_work_order()


    def operation_cards(self):
        """Every Master Job Card raised for this operation on the same order.

        More than one once part production starts: the card the order was raised with,
        and a pending card for each balance carried on from it."""
        return frappe.get_all(
            "Master Job Card",
            filters={
                "master_work_order_number": self.master_work_order_number,
                "operation_name": self.operation_name,
                "docstatus": ["<", 2],
            },
            fields=["name", "status", "total_actual_time"],
            order_by="creation",
        )

    def operation_totals(self):
        """What the operation as a whole has reported, over all of its cards.

        This card is added up from the document in hand rather than from the database:
        the sync runs on save, and the rows stored against it are still a step behind.
        The others are read off their own detail rows.

        Pending is not worked out here -- it depends on what every operation before
        this one lost, which only the Master Work Order can see."""
        totals = {
            "completed_qty": 0.0,
            "process_loss_qty": 0.0,
            "rejected_qty": 0.0,
            "actual_time": 0.0,
        }
        statuses = []

        def add(rows):
            for row in rows:
                totals["completed_qty"] += flt(row.completed_qty)
                totals["process_loss_qty"] += flt(row.process_loss_qty)
                totals["rejected_qty"] += flt(row.rejected_qty)

        cards = self.operation_cards()

        # A cancelled card reported nothing in the end: the work it booked went back
        # with the Job Cards it was holding, and the operation stands where it stood
        # before the card was raised.
        if self.docstatus != 2:
            add(self.get("job_card_detail") or [])
            totals["actual_time"] += flt(self.total_actual_time)
            statuses.append(self.status)

        others = [card for card in cards if card.name != self.name]

        if others:
            rows_by_card = {}
            for row in frappe.get_all(
                "Master Job Card Detail",
                filters={
                    "parent": ["in", [card.name for card in others]],
                    "parenttype": "Master Job Card",
                },
                fields=[
                    "parent", "completed_qty", "process_loss_qty", "rejected_qty",
                ],
            ):
                rows_by_card.setdefault(row.parent, []).append(row)

            for card in others:
                add(rows_by_card.get(card.name) or [])
                totals["actual_time"] += flt(card.total_actual_time)
                statuses.append(card.status)

        totals["status"] = operation_status(statuses)

        return totals

    def sync_to_master_work_order(self):
        if not self.master_work_order_number or not self.operation_name:
            return

        # In-House, because a card is only ever an In-House line's -- and the same
        # operation may also be on the order as a supplier's, whose figures are the
        # Purchase Order's and not this card's.
        row = frappe.db.get_value(
            "Master Work Order Operation",
            {
                "parent": self.master_work_order_number,
                "parenttype": "Master Work Order",
                "opration_name": self.operation_name,
                "manufacturing_type": "In-House",
            },
            "name",
        )
        if not row:
            return

        totals = self.operation_totals()
        hour_rate = flt(self.hour_rate)

        # Total Qty to Manufacture is left alone: it is the order's own figure, set
        # when the operations were fetched, so the row keeps showing what was asked
        # for beside what was actually run.
        frappe.db.set_value(
            "Master Work Order Operation",
            row,
            {
                "status": totals["status"],
                # Completed, Process Loss and Pending are written together a moment
                # later, in update_operation_rows(), off one reckoning -- so the
                # three always agree and Pending is never left over from an older
                # one. Only what the card alone knows is set here.
                "actual_time": totals["actual_time"],
                "hour_rate": hour_rate,
                "operating_cost": flt((totals["actual_time"] / 60.0) * hour_rate, 2),
            },
            update_modified=False,
        )

        self.update_master_work_order_process_loss()

        # Every operation, not just this one: a piece lost here never reaches any of
        # the operations after it, so their pending qty moves too. The item rows'
        # status comes with it -- an operation reporting is what moves them off Not
        # Started, and the Finish is too late to wait for.
        master_work_order = frappe.get_doc(
            "Master Work Order", self.master_work_order_number
        )
        master_work_order.update_operation_rows()
        # The loss reaches the Work Order here rather than waiting for the Finish:
        # an order destroyed outright is never finished, so nothing else would ever
        # carry it, and the Work Order would sit In Process for good.
        master_work_order.hold_process_loss_to_actual()
        master_work_order.refresh_item_status()
        master_work_order.set_status_from_work_orders()

        self.move_master_work_order_off_not_started()

    def operation_loss_by_work_order(self):
        """Loss the completed operations have reported, per Work Order.

        Completed only: a card still running has reported nothing final, and its
        figures move until it is finished."""
        cards = frappe.get_all(
            "Master Job Card",
            filters={
                "master_work_order_number": self.master_work_order_number,
                "docstatus": 1,
                "status": "Completed",
            },
            pluck="name",
        )
        if not cards:
            return {}

        lost = {}
        for row in frappe.get_all(
            "Master Job Card Detail",
            filters={"parent": ["in", cards], "parenttype": "Master Job Card"},
            fields=["work_order_number", "process_loss_qty", "rejected_qty"],
        ):
            if not row.work_order_number:
                continue
            lost[row.work_order_number] = lost.get(row.work_order_number, 0.0) + (
                flt(row.process_loss_qty) + flt(row.rejected_qty)
            )

        return lost

    def update_master_work_order_process_loss(self):
        """Write the operations' loss onto the order: per item, and as a total.

        The Master Job Cards are the only source of this figure."""
        lost = self.operation_loss_by_work_order()
        total = 0.0

        for row in frappe.get_all(
            "Master Work Order Item",
            filters={
                "parent": self.master_work_order_number,
                "parenttype": "Master Work Order",
            },
            fields=["name", "work_order_number", "process_loss_qty", "qty_to_manufacture",
                    "manufacture_qty"],
        ):
            value = flt(lost.get(row.work_order_number), 3)
            total += value

            if flt(row.process_loss_qty) == value:
                continue

            frappe.db.set_value(
                "Master Work Order Item", row.name, {
                    "process_loss_qty": value,
                    "pending_qty": max(
                        flt(row.qty_to_manufacture) - flt(row.manufacture_qty) - value, 0.0
                    ),
                },
                update_modified=False,
            )

        frappe.db.set_value(
            "Master Work Order", self.master_work_order_number,
            "total_process_loss", flt(total, 3), update_modified=False,
        )

    def move_master_work_order_off_not_started(self):
        """Work reported here means the order has started, whatever route the material
        took. Only this step -- Completed is the Work Orders' call, on the Finish."""
        if OPERATION_STATUS.get(self.status, "Pending") == "Pending":
            return

        # Work has begun, so the order is stamped with the date it began -- start
        # and end are a pair on the form, and the end date is always stamped. Set
        # here rather than only on the material transfer, which an order that skips
        # the WIP warehouse never runs, leaving the field empty for the whole run.
        if not frappe.db.get_value(
            "Master Work Order", self.master_work_order_number, "actual_start_date"
        ):
            frappe.db.set_value(
                "Master Work Order",
                self.master_work_order_number,
                "actual_start_date",
                now_datetime(),
                update_modified=False,
            )

        if frappe.db.get_value(
            "Master Work Order", self.master_work_order_number, "status"
        ) != "Not Started":
            return

        frappe.db.set_value(
            "Master Work Order",
            self.master_work_order_number,
            "status",
            "In Process",
            update_modified=False,
        )

    def sync_employees_to_job_cards(self):
        employees = {e.employee for e in (self.get("employee") or []) if e.employee}

        for row in (self.get("job_card_detail") or []):
            if not row.job_card_number:
                continue

            job_card = frappe.get_doc("Job Card", row.job_card_number)
            if job_card.docstatus != 0:
                continue

            current = {e.employee for e in (job_card.get("employee") or []) if e.employee}
            if current == employees:
                continue

            job_card.set("employee", [{"employee": emp} for emp in sorted(employees)])
            job_card.save(ignore_permissions=True)

    def submit_completed_job_cards(self):
        """Submit the Job Cards the operation has finished with.

        A card asked for nothing is finished with: an operation that runs one item and
        leaves another for a later pass reports the second at zero throughout -- Qty
        to Manufacture, Completed, Process Loss and Rejected all nothing -- and there
        is no work left on it to wait for. It is submitted at zero, which ERPNext
        allows: validate_job_card() asks only that the Work Order is not Stopped and
        that there are time logs, and validate_transfer_qty() compares transferred
        against for_quantity, which is nought against nought.

        Submitting it is what lets this card go. validate_jobs_completed() will not
        pass a card while a Job Card it names is unsubmitted, so a zero row left in
        draft used to hold the whole operation open with no way to close it.

        Still skipped where the card was asked for something and reported nothing:
        that is not a row being passed on, it is a row nobody filled in, and it should
        stop the submit the way it always has."""
        for row in (self.get("job_card_detail") or []):
            if not row.job_card_number:
                continue

            job_card = frappe.get_doc("Job Card", row.job_card_number)
            if job_card.docstatus != 0:
                continue
            if flt(job_card.total_completed_qty) <= 0 and flt(job_card.for_quantity) > 0:
                continue

            job_card.submit()

    def validate_jobs_completed(self):
        pending = [
            row.job_card_number
            for row in (self.get("job_card_detail") or [])
            if row.job_card_number
            and frappe.db.get_value("Job Card", row.job_card_number, "docstatus") != 1
        ]
        if not pending:
            return

        frappe.throw(
            ("No work has been reported against these Job Cards:<br><br>{0}<br><br>"
             "Use <b>Job &gt; Complete</b> to finish the operation -- that reports the "
             "qty, submits the Job Cards and submits this card, all together.").format(
                "<br>".join(
                    frappe.utils.get_link_to_form("Job Card", name) for name in pending
                )
            ),
            title="Operation Not Complete",
        )

    # ------------------------------------------------------------------
    # Job Cards
    # ------------------------------------------------------------------
    def release_back_links(self):
        """Take this card's name off every other Master Job Card that names it.

        The cards of an order are raised in a chain, each one pointing back at the
        operation before it, and a cancelled card is no longer an operation before
        anything: previous_operation_ceiling() would go on holding the card after it
        to a figure nobody ran. Cleared rather than re-pointed at this card's own
        predecessor -- what the cancelled operation would have turned out is not
        known, so the card after it is left unbound and keeps the quantity the order
        was raised for.

        In before_cancel, and it has to be: Frappe refuses to cancel a document a
        submitted one links to (check_no_back_links_exist), so a submitted card
        further down the chain would block this one outright.

        Every card, whatever its docstatus. A draft one reads the link the moment it
        is fetched, and a submitted one is what would block the cancel."""
        names = frappe.get_all(
            "Master Job Card",
            filters={"previous_opration_master_job_card": self.name},
            pluck="name",
        )

        for name in names:
            frappe.db.set_value(
                "Master Job Card",
                name,
                "previous_opration_master_job_card",
                None,
                update_modified=False,
            )

        return names

    def link_job_cards(self):
        """Take up a Job Card of the Work Order for each row of this card.

        ERPNext raises one per operation row when the Work Order is submitted, so the
        first Master Job Card of an operation claims those rather than raising any of
        its own. A pending card finds them all taken -- the balance it carries was
        never raised for -- and one is raised for it here, for its qty alone."""
        missing = []

        for row in (self.get("job_card_detail") or []):
            if row.job_card_number:
                continue
            if not row.work_order_number:
                frappe.throw(
                    ("Row {0}: Work Order is required to link a Job Card.").format(row.idx)
                )

            job_card = frappe.db.get_value(
                "Job Card",
                {
                    "work_order": row.work_order_number,
                    "operation": self.operation_name,
                    "master_job_card": ["is", "not set"],
                    "docstatus": ["<", 2],
                },
                "name",
                order_by="creation",
            ) or self.raise_job_card(row)

            if not job_card:
                missing.append(row)
                continue

            row.db_set("job_card_number", job_card, update_modified=False)
            frappe.db.set_value(
                "Job Card", job_card, "master_job_card", self.name, update_modified=False
            )

        if missing:
            frappe.throw(
                ("No Job Card could be raised for {0}:<br><br>{1}<br><br>"
                 "The operation is not on the Work Order, so there is no operation row "
                 "to book the work against.").format(
                    frappe.bold(self.operation_name),
                    "<br>".join(
                        "Row {0}: {1} -- {2}".format(
                            row.idx,
                            row.item_code or "",
                            frappe.utils.get_link_to_form(
                                "Work Order", row.work_order_number
                            ),
                        )
                        for row in missing
                    ),
                ),
                title="Job Card Not Available",
            )

    def raise_job_card(self, row):
        """A Job Card of the Work Order for this row's qty, for the part production the
        Work Order has not raised one for.

        Against the Work Order's own operation row, so ERPNext keeps counting the
        operation the way it always has: get_current_operation_data() sums the
        completed qty of every submitted card sharing an operation_id, and the balance
        run here lands on the same total as the run before it."""
        from erpnext.manufacturing.doctype.work_order.work_order import create_job_card

        work_order = frappe.get_doc("Work Order", row.work_order_number)
        if work_order.docstatus != 1:
            return None

        operation = next(
            (op for op in work_order.operations if op.operation == self.operation_name),
            None,
        )
        if not operation:
            return None

        # Not a stored field -- ERPNext sets it on the row in
        # split_qty_based_on_batch_size() before create_job_card() reads it, and the
        # whole of this card's qty goes on the one Job Card.
        operation.job_card_qty = flt(row.qty_to_manufacture) or flt(work_order.qty)

        return create_job_card(work_order, operation, auto_create=True).name

    def linked_job_cards(self):
        """Every Job Card this card holds: named on its rows, stamped with its name,
        or both.

        The two come apart, and a Job Card missed either way is one the cancel then
        cannot get past -- Frappe reads the stamp, not the rows
        (check_no_back_links_exist), and refuses with "Cannot delete or cancel
        because Job Card X is linked with Master Job Card Y". Ways they come apart:

            fetch_from_master_work_order() rebuilds the detail table from scratch, so
            re-fetching a card that has already claimed its Job Cards leaves them
            stamped and named on no row;

            _apply_previous_operation_ceiling() and apply_pending_qty() drop a row
            outright where the operation has nothing left to run, stamp and all.

        The stamp is the one that decides, so the stamp is what is read. The rows go
        first only so the cancel works the operation in the order the card lists it."""
        names = []
        seen = set()

        for row in (self.get("job_card_detail") or []):
            if row.job_card_number and row.job_card_number not in seen:
                seen.add(row.job_card_number)
                names.append(row.job_card_number)

        for name in frappe.get_all(
            "Job Card",
            filters={"master_job_card": self.name},
            pluck="name",
            order_by="creation",
        ):
            if name not in seen:
                seen.add(name)
                names.append(name)

        return [name for name in names if frappe.db.exists("Job Card", name)]

    def release_job_cards(self):
        """Hand the Job Cards back. They belong to the Work Order, not to this card,
        so cancelling here only releases them -- a submitted one is cancelled first,
        since the qty it reported was reported through this card.

        Returns the Stock Entries cancelled along the way, for the one report the
        cancel puts out."""
        cancelled = []

        for name in self.linked_job_cards():
            if frappe.db.get_value("Job Card", name, "docstatus") == 1:
                cancelled += self.cancel_job_card_stock_entries(name)
                self.cancel_job_card_inspections(name)
                # Read only now, and never before the two above. Cancelling either
                # writes back onto the Job Card -- a Stock Entry through
                # set_transferred_qty(), an inspection through its own status hook --
                # so a doc read ahead of them is stale by the time it is used, and
                # cancel() refuses it with a timestamp mismatch rather than a word
                # about the operation.
                self.cancel_job_card(frappe.get_doc("Job Card", name))

            frappe.db.set_value(
                "Job Card", name, "master_job_card", None, update_modified=False
            )

        return cancelled

    def cancel_job_card_stock_entries(self, job_card_number):
        """Put back the material drawn against one Job Card.

        Material Transfer On decides where the raw material is moved: onto the Work
        Order, in which case the entries are the order's and are cancelled from it,
        or onto the Job Card itself, in which case they carry the Job Card's name and
        are this card's to undo. Two reasons to undo them, and either alone would be
        enough: Frappe will not cancel a document a submitted one links to, so the
        Job Card cannot be released while they stand; and the operation they were
        drawn for is being cancelled, so the material has to come back regardless.

        Only for a Job Card that is itself being cancelled. A draft one is merely
        released and stays on the Work Order for the next Master Job Card to claim,
        so what has already reached it stays where it is -- taking it back would
        leave the next card transferring material that never left."""
        return self.cancel_stock_entries({"job_card": job_card_number, "docstatus": 1})

    def cancel_job_card_inspections(self, job_card_number):
        """Cancel the Quality Inspections taken against one Job Card.

        make_quality_inspection() raises them against the Job Card, and Quality
        Inspection points back at it through a Dynamic Link -- so a submitted one
        stops the Job Card being cancelled just as surely as a Stock Entry does, and
        with the same unhelpful message. They are cancelled rather than worked around
        because an inspection of work that is being undone is an inspection of
        nothing; the record stands, cancelled, and the reading it took is still
        readable on it.

        Cancelling one runs update_master_job_card_detail(), which takes its name
        back off the detail row it was written onto -- so the rows are tidied by the
        same step."""
        inspections = frappe.get_all(
            "Quality Inspection",
            filters={
                "reference_type": "Job Card",
                "reference_name": job_card_number,
                "docstatus": 1,
            },
            pluck="name",
        )

        for name in inspections:
            frappe.get_doc("Quality Inspection", name).cancel()

        return inspections

    def cancel_job_card(self, job_card):
        """Cancel one Job Card, and say what to do where ERPNext will not have it.

        It refuses while the operation backs production that has been booked:
        validate_produced_quantity() will not leave the Work Order having produced
        more than its operations account for. Nothing here overrules that -- the
        Manufacture entries belong to the Master Work Order and are cancelled from
        it -- but ERPNext's own message names the Work Order, which is not where they
        were raised. Named properly, the refusal is a step to take.

        validate_cancel() has already turned back the finished order; what reaches
        here is the part-finished one, which carries booked production without ever
        having reached Completed."""
        from erpnext.manufacturing.doctype.job_card.job_card import JobCardCancelError

        try:
            job_card.cancel()
        except JobCardCancelError:
            entries = self.manufacture_entries()

            frappe.throw(
                ("{0} has been produced against, so its Job Card {1} cannot be "
                 "cancelled -- the Work Order would be left reporting goods that no "
                 "operation accounts for.<br><br>"
                 "Cancel the Manufacture Stock Entry(s) raised by the Finish on {2} "
                 "first, then cancel this card:<br>{3}").format(
                    frappe.bold(self.operation_name or "This operation"),
                    frappe.bold(job_card.name),
                    frappe.utils.get_link_to_form(
                        "Master Work Order", self.master_work_order_number
                    ),
                    "<br>".join(
                        frappe.utils.get_link_to_form("Stock Entry", entry)
                        for entry in entries
                    ) or "(none found against this order)",
                ),
                title="Production Booked Against This Operation",
            )

    # ------------------------------------------------------------------
    # Semi-finished goods
    # ------------------------------------------------------------------
    @frappe.whitelist()
    def sfg_item_rows(self):
        """The finished goods of this operation -- the same list either way."""
        warehouse = self.sfg_warehouse or self.wip_warehouse

        return [
            {
                "item_code": row.item_code,
                "item_name": row.item_name,
                "uom": row.uom,
                "warehouse": warehouse,
                "qty": flt(row.completed_qty) or flt(row.qty_to_manufacture),
            }
            for row in (self.get("job_card_detail") or [])
            if row.item_code
        ]

    @frappe.whitelist()
    def make_sfg_stock_entry(self, entry_type, rows=None):
        """Post the semi-finished goods, and log what was posted.

        Stock Out puts them into store, so it is a receipt. Stock In hands them back
        to the floor to be worked, so it is a consumption."""
        from erpnext.stock.get_item_details import get_conversion_factor

        purpose = SFG_STOCK_ENTRY_TYPE.get(entry_type)
        if not purpose:
            frappe.throw(("{0} is not a semi-finished goods entry.").format(entry_type))

        rows = frappe.parse_json(rows) if rows else self.sfg_item_rows()
        incoming = entry_type == "Stock Out"

        stock_entry = frappe.new_doc("Stock Entry")
        stock_entry.stock_entry_type = purpose
        stock_entry.purpose = purpose
        stock_entry.company = self.company
        stock_entry.master_job_card = self.name
        stock_entry.master_work_order = self.master_work_order_number
        stock_entry.project = self.get("project")

        booked = []
        for row in rows:
            qty = flt(row.get("qty"))
            if qty <= 0:
                continue

            item_code = row.get("item_code")
            warehouse = (
                row.get("warehouse") or self.fg_warehouse
            )
            if not warehouse:
                frappe.throw(
                    ("{0}: a Warehouse is needed for this entry.").format(item_code)
                )

            stock_uom = frappe.db.get_value("Item", item_code, "stock_uom")
            uom = row.get("uom") or stock_uom
            conversion_factor = (
                flt(get_conversion_factor(item_code, uom).get("conversion_factor")) or 1.0
            )

            stock_entry.append("items", {
                "item_code": item_code,
                "qty": qty,
                "uom": uom,
                "stock_uom": stock_uom,
                "conversion_factor": conversion_factor,
                "t_warehouse": warehouse if incoming else None,
                "s_warehouse": None if incoming else warehouse,
            })
            booked.append({
                "item_code": item_code,
                "uom": uom,
                "qty": qty,
                "warehouse": warehouse,
            })

        if not stock_entry.get("items"):
            frappe.throw("Enter a qty for at least one item.", title="Nothing to Post")

        stock_entry.insert()
        stock_entry.submit()

        for row in booked:
            self.append("sfg_stock", {
                "entry_type": entry_type,
                "item_code": row["item_code"],
                "uom": row["uom"],
                "qty": row["qty"],
                "to_warehouse": row["warehouse"] if incoming else None,
                "from_warehouse": None if incoming else row["warehouse"],
                "stock_entry_reference": stock_entry.name,
                "posting_date": frappe.utils.now_datetime(),
            })

        self.save_after_submit()

        frappe.msgprint(
            ("{0} posted on {1}.").format(
                entry_type,
                frappe.utils.get_link_to_form("Stock Entry", stock_entry.name),
            ),
            indicator="green",
            alert=True,
        )

        return stock_entry.name

    # ------------------------------------------------------------------
    # Quality Inspection
    # ------------------------------------------------------------------
    @frappe.whitelist()
    def make_quality_inspection(self, detail_row):
        row = next(
            (r for r in (self.get("job_card_detail") or []) if r.name == detail_row),
            None,
        )
        if not row:
            frappe.throw(("Job Card Detail row {0} not found.").format(detail_row))

        if row.quality_inspection:
            frappe.throw(
                ("Row {0} already has Quality Inspection {1}.").format(
                    row.idx, row.quality_inspection
                )
            )

        if not row.job_card_number:
            frappe.throw(
                ("Row {0}: the Job Card has not been created yet, so there is "
                 "nothing to inspect against.").format(row.idx)
            )

        quality_inspection = frappe.new_doc("Quality Inspection")
        quality_inspection.inspection_type = "In Process"
        quality_inspection.reference_type = "Job Card"
        quality_inspection.reference_name = row.job_card_number
        quality_inspection.item_code = row.item_code
        quality_inspection.item_name = row.item_name
        quality_inspection.batch_no = row.batch_no
        quality_inspection.sample_size = flt(row.qty_to_manufacture)
        quality_inspection.bom_no = row.bom_no
        quality_inspection.quality_inspection_template = self.quality_inspection_template
        quality_inspection.company = self.company
        quality_inspection.inspected_by = frappe.session.user
        quality_inspection.get_item_specification_details()

        return quality_inspection

    # ------------------------------------------------------------------
    # Start / Pause / Resume / Complete -- applied on all linked Job Cards
    # ------------------------------------------------------------------
    @frappe.whitelist()
    def start_jobs(self, employees=None):
        # First, and before start_operators() -- that saves the card, and the save
        # runs sync_to_master_work_order(), which moves the order to In Process by
        # itself. Checked afterwards the guard would be reading the status its own
        # run had just written, and would never refuse anything.
        self.validate_master_work_order_started()

        self.start_operators(employees)
        self.drive_job_cards("start")
        self.db_set("actual_start_date", now_datetime())
        self.set_card_status("Work In Progress")

    def validate_master_work_order_started(self):
        """The order has to have been started before the work under it is.

        The order's own Start is the material transfer to WIP, which is what moves it
        to In Process -- so until it has been pressed the material this operation
        would consume has not reached the floor, and a card started before it books
        time against work that cannot yet be done."""
        if not self.master_work_order_number:
            return

        order = frappe.db.get_value(
            "Master Work Order",
            self.master_work_order_number,
            ["status", "material_transfer_on"],
            as_dict=True,
        )
        if not order:
            return

        # An order whose material moves on the Job Card has no Start of its own --
        # add_start_button() does not offer one -- so the only thing that can move it
        # to In Process is this card. Held to the rule it would never start at all.
        if order.material_transfer_on == "Job Card":
            return

        if order.status == "In Process":
            return

        frappe.throw(
            ("Master Work Order {0} is {1}.<br><br>"
             "Please start the Master Work Order before starting the "
             "linked Job Card.").format(
                frappe.utils.get_link_to_form(
                    "Master Work Order", self.master_work_order_number
                ),
                frappe.bold(order.status or "Draft"),
            ),
            title="Master Work Order Not In Process",
        )

    def start_operators(self, employees):
        if isinstance(employees, str):
            employees = frappe.parse_json(employees)
        employees = employees or []

        existing = {e.employee for e in (self.get("employee") or [])}

        operators = []
        for emp in employees:
            emp_id = (emp.get("employee") or emp.get("name")) if isinstance(emp, dict) else emp
            if not emp_id:
                continue

            if emp_id not in existing:
                self.append("employee", {"employee": emp_id})
                existing.add(emp_id)

            operators.append(emp_id)

        self.open_time_logs(operators)
        self.save_after_submit()

    def open_time_logs(self, operators):
        """Open a Time Log row per operator per Job Card.

        Per Job Card, not merely per operator: calculate_detail_rows() rolls the logs
        up keyed on job_card_number, so a row without one contributes nothing. That is
        why completed qty and actual time never moved off zero -- the rows were being
        written with only an employee and a start time.

        An operator is optional. With none named the run still gets one unattributed
        row per Job Card, so the time and the qty it reports are still booked."""
        operators = list(operators) or [None]
        now = frappe.utils.now()

        for row in (self.get("job_card_detail") or []):
            if not row.job_card_number:
                continue

            for emp_id in operators:
                self.append("time_log", {
                    "employee": emp_id,
                    "job_card_number": row.job_card_number,
                    "item_code": row.item_code,
                    "from_time": now,
                })

    @frappe.whitelist()
    def pause_jobs(self, reason=None, rows=None):
        """Book whatever has been made before stopping, so a pause does not lose the
        progress of the run it interrupts."""
        self.book_reported_qty(rows)
        self.drive_job_cards("pause")
        self.db_set("hold_reason", reason)
        self.set_card_status("On Hold")

    @frappe.whitelist()
    def resume_jobs(self):
        self.drive_job_cards("resume")
        self.open_operator_time_logs()
        self.set_card_status("Work In Progress")

    @frappe.whitelist()
    def complete_jobs(self, rows=None):
        self.validate_complete_qty(rows)
        self.book_reported_qty(rows)
        self.drive_job_cards("complete")

        completed = 0
        for row in self.job_card_detail:
            completed += row.completed_qty

        self.db_set("total_completed_qty", completed)

        blocked = self.job_cards_blocking_submit()
        if not blocked:
            self.submit()
            return

        # Work reported, but material has not reached WIP -- stay in draft.
        self.set_card_status("Completed")
        frappe.msgprint(
            ("The operation is complete, but this card cannot be submitted yet -- "
             "material still has to reach the WIP Warehouse for:<br><br>{0}<br><br>"
             "Transfer it, then submit this card.").format(
                "<br>".join(
                    frappe.utils.get_link_to_form("Job Card", name) for name in blocked
                )
            ),
            title="Material Transfer Pending",
            indicator="orange",
        )

    def validate_complete_qty(self, rows):
        """Completing has to account for every piece the card was raised for -- made,
        lost, or explicitly left pending.

        Pending is what makes part production work: finishing 6 of 10 is fine as long
        as the other 4 are declared, and Create Pending Master Job Card then carries
        exactly that balance onto a fresh card. What is not allowed is qty simply
        going missing, or more being booked than was ever ordered. Same rule ERPNext
        applies to a Job Card in validate_job_card()."""
        rows = frappe.parse_json(rows) if rows else []
        if not rows:
            return

        detail = {
            row.job_card_number: row
            for row in (self.get("job_card_detail") or [])
            if row.job_card_number
        }

        caps = self.qty_caps()

        for data in rows:
            row = detail.get(data.get("job_card_number"))
            if not row:
                continue

            # The dialog's own figure -- it is editable there, and what is reported
            # has to fit inside what the run is being held to. Taken as sent, 0 and
            # all: complete_jobs_dialog() puts a figure on every row it sends, so
            # there is nothing to fall back to the stored qty for, and falling back
            # was what threw a typed 0 away and checked the row against the 10 the
            # operation was raised for.
            #
            # A row asked for nothing needs no exemption from either check below. The
            # cap is never negative -- qty_caps() floors it at 0 -- so nothing can be
            # over it, and nothing reported cannot be more than nothing asked. Left
            # to them rather than skipped, so a caller that sends no qty at all still
            # has to answer for whatever it reports.
            ordered = flt(data.get("qty_to_manufacture"))

            # The same ceiling the dialog applies, checked again here. The dialog is
            # the only thing that was enforcing it, so a stale form or a direct call
            # could book more than the order has left and leave the item reading a
            # negative pending qty.
            cap = caps.get(row.work_order_number)
            if cap is not None and ordered > flt(cap) + 0.001:
                frappe.throw(
                    ("{0}: at most {1} can be made. The order asked for {2} and the "
                     "other operations have lost the rest.").format(
                        row.item_code or row.job_card_number,
                        flt(cap, 3),
                        flt(row.qty_to_manufacture, 3),
                    ),
                    title="Qty to Manufacture Too High",
                )

            # What the row has already used up, plus everything this run reports.
            total = accounted_qty(data)

            if total <= ordered + 0.001:
                continue

            frappe.throw(
                ("{0}: {1} accounted for against a Qty to Manufacture of {2}.<br><br>"
                 "Completing has to account for the whole quantity -- report the "
                 "balance as Completed, Process Loss, or Pending to carry it to a "
                 "new Master Job Card.").format(
                    row.item_code or row.job_card_number,
                    flt(total, 3),
                    flt(ordered, 3),
                ),
                title="Quantity Does Not Add Up",
            )

    def book_reported_qty(self, rows):
        rows = frappe.parse_json(rows) if rows else []
        if not rows:
            return

        by_job_card = {
            row.get("job_card_number"): row for row in rows if row.get("job_card_number")
        }
        if not by_job_card:
            return

        now = frappe.utils.now()
        booked = set()

        for log in self.time_log:
            if log.to_time:
                continue
            data = by_job_card.get(log.job_card_number)
            if not data:
                continue

            log.completed_qty = flt(data.get("completed_qty"))
            log.rejected_qty = flt(data.get("rejected_qty"))
            log.to_time = now

            booked.add(log.job_card_number)

        for row in self.job_card_detail:
            # A row the dialog did not report on keeps what it already had -- Pause
            # sends only the Job Cards that were running.
            data = by_job_card.get(row.job_card_number)
            if not data:
                continue

            # Qty to Manufacture is editable in the dialog, so carry it back too.
            # Without this the row keeps the figure the operation was raised for, the
            # save has nothing to push, and the Job Card stays at its old For Quantity
            # -- 10 where the run was only ever for 5, leaving 5 pending on it for good.
            if data.get("qty_to_manufacture") is not None:
                row.qty_to_manufacture = flt(data.get("qty_to_manufacture"))

            row.completed_qty = flt(data.get("completed_qty"))
            row.rejected_qty = flt(data.get("rejected_qty"))
            row.process_loss_qty = flt(data.get("process_loss_qty"))
            row.rejection_reason = data.get("rejection_reason")

        self.save_after_submit()
    def job_cards_blocking_submit(self):
        """Job Cards ERPNext will not let us submit yet.

        Its rule, from validate_transfer_qty(): a card carrying material rows of its
        own cannot be submitted until the transferred qty covers what it was raised
        for."""
        blocked = []

        for row in (self.get("job_card_detail") or []):
            if not row.job_card_number:
                continue

            job_card = frappe.db.get_value(
                "Job Card",
                row.job_card_number,
                [
                    "name", "docstatus", "for_quantity", "transferred_qty",
                    "finished_good", "is_corrective_job_card",
                ],
                as_dict=True,
            )
            if not job_card or job_card.docstatus != 0:
                continue
            if job_card.finished_good or job_card.is_corrective_job_card:
                continue
            if not frappe.db.count("Job Card Item", {"parent": job_card.name}):
                continue

            if flt(job_card.transferred_qty) < flt(job_card.for_quantity):
                blocked.append(job_card.name)

        return blocked

    def close_open_time_logs(self):
        """Set to_time = now on any open Time Log row (from_time set, no to_time)."""
        now = frappe.utils.now()
        changed = False
        for log in self.time_log:
            if log.from_time and not log.to_time:
                log.to_time = now
                changed = True
        if changed:
            self.save_after_submit()

    def open_operator_time_logs(self):
        """Resume: a fresh Time Log row for each assigned operator, on each Job Card."""
        operators = [e.employee for e in (self.get("employee") or []) if e.employee]
        self.open_time_logs(operators)
        self.save_after_submit()

    def save_after_submit(self):
        # This card keeps being updated after submit (time logs, totals), so skip the
        # "cannot change after submit" guard. Frappe routes this save through
        # update_after_submit, so the totals are refreshed by
        # before_update_after_submit() rather than before_save().
        self.flags.ignore_validate_update_after_submit = True
        self.save()

    def drive_job_cards(self, action):
        details = [r for r in (self.get("job_card_detail") or []) if r.job_card_number]
        if not details:
            frappe.throw("No linked Job Cards to process.")

        employees = [{"employee": e.employee} for e in (self.get("employee") or []) if e.employee]
        # An operator is optional. ERPNext writes a Time Log row per employee handed
        # to it and none at all for an empty list, and a Job Card with no time logs
        # cannot be submitted (validate_time_logs_present) and reports a completed qty
        # of zero. One blank operator keeps that row -- unattributed, but there.
        start_employees = employees or [{"employee": None}]

        now = frappe.utils.now()

        for detail in details:
            job_card = frappe.get_doc("Job Card", detail.job_card_number)
            # ERPNext allows start/pause/resume/complete only on draft Job Cards.
            if job_card.docstatus != 0:
                continue

            if action == "start":
                job_card.start_timer(start_time=now, employees=start_employees)
            elif action == "pause":
                job_card.pause_job(end_time=now)
            elif action == "resume":
                job_card.resume_job(start_time=now)
            elif action == "complete":
                qty = flt(detail.completed_qty)
                process_loss = flt(detail.process_loss_qty) + flt(detail.rejected_qty)
                pending = max(flt(job_card.for_quantity) - qty - process_loss, 0.0)

                job_card.complete_job_card(
                    end_time=now,
                    qty=qty,
                    process_loss_qty=process_loss,
                    pending_qty=pending,
                )

        frappe.msgprint(
            (f"{action.title()} Master Job Card Successfully."),
            indicator="green",
            alert=True,
        )


    @frappe.whitelist()
    def fetch_from_master_work_order(self):
        if not self.master_work_order_number:
            return

        if not self.operation_name:
            frappe.throw(("Please select an Operation Name first."))

        mwo = frappe.get_doc("Master Work Order", self.master_work_order_number)

        self._set_header_from_mwo(mwo)
        self._set_operation_details(mwo)
        self._set_detail_rows(mwo)
        self._apply_previous_operation_ceiling()

        self.calculate_detail_rows()
        self.calculate_scrap_items()
        self.calculate_totals()

    def apply_pending_qty(self, qty_by_item):
        """Hold this card to the picked rows, keyed by item. The balance was worked
        out in MasterWorkOrder.pending_master_job_card_rows() -- no rule here. An
        item that was not picked drops out entirely: there is no work there to
        raise a Job Card against."""
        rows = []
        for row in (self.get("job_card_detail") or []):
            qty = flt(qty_by_item.get(row.item_code))
            if qty <= 0:
                continue

            row.qty_to_manufacture = qty
            rows.append(row)

        if not rows:
            frappe.throw(
                ("Nothing is left to run through {0}.").format(
                    frappe.bold(self.operation_name)
                ),
                title="Nothing Pending",
            )

        self.set("job_card_detail", rows)

    def _set_header_from_mwo(self, mwo):
        if not self.posting_date:
            self.posting_date = now_datetime()

        self.company = mwo.company
        self.production_plan_number = mwo.production_plan_number
        self.project = mwo.get("project")
        self.cost_center = mwo.get("cost_center")
        self.material_transfer_on = mwo.material_transfer_on

        self.source_warehouse = mwo.source_warehouse
        self.wip_warehouse = mwo.wip_warehouse
        self.fg_warehouse = mwo.fg_warehouse
        self.scrap_warehouse = mwo.scrap_warehouse
        self.sfg_warehouse = mwo.get("sfg_warehouse")

        self.expected_start_date = mwo.get("planned_start_date")
        self.expected_end_date = mwo.get("planned_end_date")

    def _get_mwo_operation(self, mwo):
        """The order's In-House line for this card's operation.

        An operation may be on the order twice, once per Manufacturing Type -- run in
        house on two of the colours and sent out on the third -- and a card is only
        ever the In-House line's. Operation Name + Manufacturing Type is unique, so
        there is at most one of those and naming the operation alone still says which
        line the card belongs to.

        Where the operation is on the order but not in house at all, the Out House row
        is handed back so _set_operation_details() can say so."""
        for op in mwo.operations:
            if (
                op.opration_name == self.operation_name
                and op.manufacturing_type == "In-House"
            ):
                return op

        for op in mwo.operations:
            if op.opration_name == self.operation_name:
                return op

        return None

    def _set_operation_details(self, mwo):
        op = self._get_mwo_operation(mwo)
        if not op:
            frappe.throw(
                ("Operation {0} is not part of Master Work Order {1}.").format(
                    frappe.bold(self.operation_name), self.master_work_order_number
                )
            )
        if op.manufacturing_type != "In-House":
            frappe.throw(
                ("Operation {0} is Out House. Master Job Card is created only "
                 "for In-House operations.").format(frappe.bold(self.operation_name))
            )

        self.manufacturing_type = "In-House"
        self.opration_sequence_no = op.opration_sequence_no
        self.workstation_type = op.workstation_type
        self.workstation = op.workstation
        self.hour_rate = op.get("hour_rate") or frappe.db.get_value(
            "Workstation", op.workstation, "hour_rate"
        )

        self._set_quality_inspection_from_bom(mwo)

    def _set_quality_inspection_from_bom(self, mwo):
        bom_nos = [i.bom_no for i in mwo.items_to_be_manufacture if i.bom_no]
        if not bom_nos:
            return

        bom = frappe.db.get_value(
            "BOM",
            {"name": ["in", bom_nos], "inspection_required": 1},
            ["inspection_required", "quality_inspection_template"],
            as_dict=True,
        )
        if bom:
            self.quality_inspection_requied = 1
            self.quality_inspection_template = bom.quality_inspection_template

    def _bom_operation_rows(self, bom_no):
        return frappe.get_all(
            "BOM Operation",
            filters={"parent": bom_no, "parenttype": "BOM"},
            fields=["operation", "workstation", "time_in_mins"],
        )

    def _set_detail_rows(self, mwo):
        """One row per Work Order that runs this operation.

        Which Work Orders those are is read off their own operation rows, not off the
        BOMs. The Work Order Operation row is the only thing a Job Card can be booked
        against, so an item runs an operation exactly when its Work Order carries it
        -- the same rule MasterWorkOrder.work_orders_running_operation() states, and
        the same one add_work_order_operation() acts on when it puts an operation the
        planner added by hand onto every Work Order of the order.

        Reading the BOMs instead was right for the operations that came from a BOM and
        wrong for the ones that did not. A hand-added operation is in no BOM at all,
        so where every item lacked it every item ran it and the answer came out right
        by accident; but where one item's BOM happened to carry the operation and
        another's did not, the second was dropped -- while its Work Order had been
        given the operation and a Job Card raised against it all the same. The card
        never claimed that Job Card, and the operation's Total Qty to Manufacture,
        counted over every Work Order running it, stood against detail rows for only
        some of them.

        The BOMs are still read, for the one thing only they know: the workstation and
        the standard time of this operation as that item runs it.

        Falling back to the BOMs while there are no Work Orders yet -- the card is
        fetched on a draft order too, where nothing has been raised to read.

        Over all of it sits the operation line's own item selection, which is the
        planner's answer rather than a reading of anything: an item the line does not
        run gets no row here, whatever its Work Order or its BOM happens to carry.
        Where the line names nothing the selection falls back to the BOM rule, so an
        order saved before the column existed builds the same card it always did."""
        self.set("job_card_detail", [])

        operation = mwo.in_house_operation(self.operation_name)
        runs = set(mwo.operation_items(operation)) if operation else None

        bom_operation = {}
        for item in mwo.items_to_be_manufacture:
            if not item.bom_no:
                continue
            bom_operation[item.name] = next(
                (r for r in self._bom_operation_rows(item.bom_no)
                 if r.operation == self.operation_name),
                None,
            )

        running = mwo.work_orders_running_operation(self.operation_name)
        from_bom = any(bom_operation.values())

        for item in mwo.items_to_be_manufacture:
            if not item.bom_no:
                continue
            if runs is not None and item.item_code not in runs:
                # The operation line does not run this item.
                continue

            bom_op = bom_operation.get(item.name)
            if running:
                if item.work_order_number not in running:
                    # This Work Order was not given the operation.
                    continue
            elif from_bom and not bom_op:
                # No Work Order to ask yet, so the BOM is all there is to go on.
                continue

            bom_op = bom_op or frappe._dict()

            item_details = frappe.get_cached_value(
                "Item", item.item_code, ["item_name", "stock_uom"], as_dict=True
            ) or frappe._dict()

            self.append("job_card_detail", {
                "item_code": item.item_code,
                "item_name": item_details.get("item_name"),
                "uom": item.get("uom") or item_details.get("stock_uom"),
                "production_plan_number": item.get("production_plan_number") or mwo.production_plan_number,
                "work_order_number": item.get("work_order_number"),
                "bom_no": item.bom_no,
                "operation_name": self.operation_name,
                "workstation": bom_op.get("workstation") or self.workstation,
                "qty_to_manufacture": item.qty_to_manufacture,
                "standerd_time": flt(bom_op.get("time_in_mins")),
                "status": "Open",
            })

    # ------------------------------------------------------------------
    # Operation sequence -- an operation can only work what the one before it made
    # ------------------------------------------------------------------
    def previous_operation_ceiling(self):
        """What the operation before this one actually turned out, per Work Order.

        Only the completed qty reaches this operation. Process loss and rejects are
        gone for good, and whatever the previous card left pending travels on its own
        pending Master Job Card, which feeds a card of this operation in its turn --
        so counting it here would let the same piece be worked twice.

        Empty while that card is still running: the figure is only known once the
        operation it measures has finished, and until then the rows keep the quantity
        the Master Work Order was raised for."""
        if not self.previous_opration_master_job_card:
            return {}

        previous = frappe.db.get_value(
            "Master Job Card",
            self.previous_opration_master_job_card,
            ["name", "status", "docstatus"],
            as_dict=True,
        )
        if not previous or previous.docstatus == 2 or previous.status != "Completed":
            return {}

        ceiling = {}
        for row in frappe.get_all(
            "Master Job Card Detail",
            filters={"parent": previous.name, "parenttype": "Master Job Card"},
            fields=["work_order_number", "completed_qty"],
        ):
            if not row.work_order_number:
                continue
            ceiling[row.work_order_number] = (
                ceiling.get(row.work_order_number, 0.0) + flt(row.completed_qty)
            )

        return ceiling

    def _apply_previous_operation_ceiling(self):
        """Hold the fetched rows to what the previous operation turned out.

        A Work Order absent from that card does not run through the previous
        operation, so nothing upstream limits it."""
        ceiling = self.previous_operation_ceiling()
        if not ceiling:
            return

        rows = []
        for row in (self.get("job_card_detail") or []):
            if row.work_order_number not in ceiling:
                rows.append(row)
                continue

            qty = min(flt(row.qty_to_manufacture), flt(ceiling[row.work_order_number]))
            if qty <= 0:
                # The previous operation turned nothing out for this Work Order --
                # there is no work here to raise a Job Card for.
                continue

            row.qty_to_manufacture = qty
            rows.append(row)

        self.set("job_card_detail", rows)

    def push_ceiling_to_next_operation(self):
        """Hand this operation's output down to the operation that follows it.

        The cards for every operation are raised together when the Master Work Order
        is submitted, so the next one is already sitting in draft carrying the figure
        the order was raised for -- 10, where this operation made 8 and lost 2. It read
        that off the Master Work Order long before this operation ran, and nothing
        would ever bring it down on its own."""
        for name in frappe.get_all(
            "Master Job Card",
            filters={
                "previous_opration_master_job_card": self.name,
                "docstatus": 0,
            },
            pluck="name",
        ):
            frappe.get_doc("Master Job Card", name).pull_ceiling_from_previous_operation()

    def pull_ceiling_from_previous_operation(self):
        ceiling = self.previous_operation_ceiling()
        if not ceiling:
            return

        changed = []
        for row in (self.get("job_card_detail") or []):
            if row.work_order_number not in ceiling:
                continue

            qty = min(flt(row.qty_to_manufacture), flt(ceiling[row.work_order_number]))

            # Work already reported here sets its own floor: an operation cannot be
            # told it was raised for less than it has already used up.
            qty = max(qty, accounted_qty(row))

            if abs(qty - flt(row.qty_to_manufacture)) <= 0.001:
                continue

            row.qty_to_manufacture = qty
            self._sync_job_card_quantity(row, qty)
            changed.append("{0}: {1}".format(row.item_code or row.idx, flt(qty, 3)))

        if not changed:
            return

        self.save_after_submit()
        frappe.msgprint(
            ("{0}: quantity brought down to what the previous operation turned out."
             "<br><br>{1}").format(
                frappe.utils.get_link_to_form("Master Job Card", self.name),
                "<br>".join(changed),
            ),
            title="Quantity Carried Forward",
            indicator="orange",
        )

    def _sync_job_card_quantity(self, row, qty):
        """Keep the Job Card in step. ERPNext measures completion against the card's
        own For Quantity -- left at the old figure it would refuse to submit at the
        new one, and job_cards_blocking_submit() would hold out for material that is
        no longer needed."""
        if not row.job_card_number:
            return

        job_card = frappe.db.get_value(
            "Job Card", row.job_card_number, ["docstatus", "for_quantity"], as_dict=True
        )
        if not job_card or job_card.docstatus != 0:
            return
        if abs(flt(job_card.for_quantity) - qty) <= 0.001:
            return

        frappe.db.set_value(
            "Job Card", row.job_card_number, "for_quantity", qty, update_modified=False
        )


    def _source_warehouse_stock(self, item_code):
        """Available stock of the item in the source warehouse (shortage check)."""
        if not self.source_warehouse:
            return 0.0
        return flt(frappe.db.get_value(
            "Bin",
            {"item_code": item_code, "warehouse": self.source_warehouse},
            "actual_qty",
        ))

    # ------------------------------------------------------------------
    # Auto-calculations
    # ------------------------------------------------------------------
    def recalculate_time_logs(self):
        for log in self.time_log:
            if log.from_time and log.to_time:
                log.time_in_mins = flt(time_diff_in_seconds(log.to_time, log.from_time)) / 60.0
            else:
                log.time_in_mins = 0

    def _job_card_transferred_qty(self):
        """How much material has reached WIP on each linked Job Card.

        ERPNext keeps the figure on the Job Card, and job_cards_blocking_submit()
        reads it there to decide whether the card may be submitted. Mirrored onto the
        detail row so the same thing holding the submit back is visible on the row it
        belongs to."""
        names = [
            row.job_card_number
            for row in (self.get("job_card_detail") or [])
            if row.job_card_number
        ]
        if not names:
            return {}

        return {
            jc.name: flt(jc.transferred_qty)
            for jc in frappe.get_all(
                "Job Card",
                filters={"name": ["in", names]},
                fields=["name", "transferred_qty"],
            )
        }

    def calculate_detail_rows(self):
        # Roll up the time logs back to the detail row they belong to (by job card):
        # actual time, and the completed / rejected qty reported against it.
        actual_by_jc = {}
        completed_by_jc = {}
        rejected_by_jc = {}
        for log in self.time_log:
            if not log.job_card_number:
                continue
            actual_by_jc[log.job_card_number] = (
                actual_by_jc.get(log.job_card_number, 0.0) + flt(log.time_in_mins)
            )
            completed_by_jc[log.job_card_number] = (
                completed_by_jc.get(log.job_card_number, 0.0) + flt(log.completed_qty)
            )
            rejected_by_jc[log.job_card_number] = (
                rejected_by_jc.get(log.job_card_number, 0.0) + flt(log.rejected_qty)
            )

        transferred_by_jc = self._job_card_transferred_qty()

        for row in (self.get("job_card_detail") or []):
            if row.job_card_number:
                # The timer owns this while the job is running. Once the card is
                # submitted the figure stands as it is, so a correction typed on the
                # row survives the next save -- a run left on overnight is put right
                # by hand, and there are no logs to put it right through.
                if self.docstatus == 0:
                    row.actual_time = actual_by_jc.get(row.job_card_number, 0.0)
                row.transferred_qty = transferred_by_jc.get(row.job_card_number, 0.0)
                if row.job_card_number in completed_by_jc:
                    row.completed_qty = completed_by_jc[row.job_card_number]
                if row.job_card_number in rejected_by_jc:
                    row.rejected_qty = rejected_by_jc[row.job_card_number]


    def calculate_scrap_items(self):
        for row in self.scrap_item:
            row.amount = flt(row.scrap_qty) * flt(row.rate)

    def calculate_sfg_stock(self):
        out_qty = sum(flt(r.qty) for r in self.sfg_stock if r.entry_type == "Stock Out")
        in_qty = sum(flt(r.qty) for r in self.sfg_stock if r.entry_type == "Stock In")
        self.total_sfg_stock_out_qty = out_qty
        self.total_sfg_stock_in_qty = in_qty
        self.sfg_balance_qty = out_qty - in_qty

    def calculate_totals(self):
        detail = (self.get("job_card_detail") or [])
        self.total_qty_to_manufacture = sum(flt(r.qty_to_manufacture) for r in detail)
        self.total_completed_qty = sum(flt(r.completed_qty) for r in detail)
        self.total_process_loss_qty = sum(flt(r.process_loss_qty) for r in detail)
        self.total_rejected_qty = sum(flt(r.rejected_qty) for r in detail)
        # Off the rows, so the card totals obey the same four-term accounting they do
        # -- taking it as qty less completed counted the losses and rejects as still
        # to be made, and the card's own totals then disagreed with its detail.
        self.total_standerd_time = sum(flt(r.standerd_time) for r in detail)
        # Off the rows rather than straight off the time logs, so an Actual Time put
        # right by hand on a submitted card carries into the operating cost and into
        # the Master Work Order. In draft the two are the same figure -- the rows are
        # filled from those very logs a moment earlier, in calculate_detail_rows().
        self.total_actual_time = flt(sum(flt(r.actual_time) for r in detail), 3)
        self.total_operating_cost = (flt(self.total_actual_time) / 60.0) * flt(self.hour_rate)

    # ------------------------------------------------------------------
    # Validations
    # ------------------------------------------------------------------
    def validate_quality_inspection(self):
        if not self.quality_inspection_requied:
            return
        for row in (self.get("job_card_detail") or []):
            if row.status == "Completed" and not row.quality_inspection:
                frappe.throw(
                    ("Row {0}: Quality Inspection is mandatory before completing "
                     "this operation.").format(row.idx)
                )


@frappe.whitelist()
def get_inhouse_operations(master_work_order):
    """Operations of the MWO restricted to In-House — used by the client query."""
    if not master_work_order:
        return []
    return [
        r.opration_name
        for r in frappe.get_all(
            "Master Work Order Operation",
            filters={"parent": master_work_order, "manufacturing_type": "In-House"},
            fields=["opration_name"],
        )
        if r.opration_name
    ]


def set_default_warehouses(row, default_warehouses):
	for field in ["wip_warehouse", "fg_warehouse", "scrap_warehouse"]:
		if not row.get(field):
			row[field] = default_warehouses.get(field)