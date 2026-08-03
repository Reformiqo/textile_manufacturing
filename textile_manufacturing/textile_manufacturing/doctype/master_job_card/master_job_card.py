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


class MasterJobCard(Document):
    def validate(self):
        self.validate_operation_is_in_house()
        self.set_actual_dates()
        self.validate_quality_inspection()
        self.validate_rejection_reason()

    def before_save(self):
        # These only compute/derive values (they don't validate anything), so
        # they run on save rather than during validation.
        self.recalculate_time_logs()
        self.calculate_detail_rows()
        self.calculate_required_items()
        self.calculate_scrap_items()
        self.calculate_sfg_stock()
        self.calculate_totals()

    def after_insert(self):
        self.link_job_cards()
        self.set_card_status("Open")

    def on_update(self):
        self.sync_employees_to_job_cards()
        self.sync_to_master_work_order()

    def before_submit(self):
        self.submit_completed_job_cards()

    def on_submit(self):
        self.validate_jobs_completed()
        self.set_card_status("Completed")

    def before_cancel(self):
        self.release_back_links()

    def on_cancel(self):
        self.release_job_cards()
        self.set_card_status("Cancelled")

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def validate_rejection_reason(self):
        """Rejected qty has to say why.

        Without it the reject is a dead number -- nobody can tell later whether it was
        a machine fault, bad yarn or an operator error, which is the only reason to
        collect reject data at all.

        The qty checked is the greater of the row's own value and what the time logs
        add up to for that Job Card. Both are needed: validate() runs before
        before_save(), so on the save that first books a reject the row has not been
        rolled up yet -- and a figure typed straight onto the row would otherwise be
        ignored, because the log total wins as soon as any log exists."""
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

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def set_card_status(self, status):
        self.db_set("status", status)
        self.sync_to_master_work_order()

        # The single point both routes to Completed pass through -- on_submit(), and
        # complete_jobs() when material still holds the submit back.
        if status == "Completed":
            self.push_ceiling_to_next_operation()

    def sync_to_master_work_order(self):
        if not self.master_work_order_number or not self.operation_name:
            return

        row = frappe.db.get_value(
            "Master Work Order Operation",
            {
                "parent": self.master_work_order_number,
                "parenttype": "Master Work Order",
                "opration_name": self.operation_name,
            },
            "name",
        )
        if not row:
            return

        completed = flt(self.total_completed_qty)

        # Actual time is logged in minutes, the rate is per hour. The rate is carried
        # over as well so the cost on the operation row can always be read back off the
        # two figures beside it -- a Workstation repriced mid-run would otherwise leave
        # a cost there that its own hour rate no longer explains.
        actual_time = flt(self.total_actual_time)
        hour_rate = flt(self.hour_rate)

        frappe.db.set_value(
            "Master Work Order Operation",
            row,
            {
                "status": OPERATION_STATUS.get(self.status, "Pending"),
                "completed_qty": completed,
                "pending_qty": flt(self.total_qty_to_manufacture) - completed,
                "actual_time": actual_time,
                "hour_rate": hour_rate,
                "operating_cost": (actual_time / 60.0) * hour_rate,
            },
            update_modified=False,
        )

        self.move_master_work_order_off_not_started()

    def move_master_work_order_off_not_started(self):
        """Work reported here means the order has started, whatever route the material
        took. Only this step -- Completed is the Work Orders' call, on the Finish."""
        if OPERATION_STATUS.get(self.status, "Pending") == "Pending":
            return

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
        for row in (self.get("job_card_detail") or []):
            if not row.job_card_number:
                continue

            job_card = frappe.get_doc("Job Card", row.job_card_number)
            if job_card.docstatus != 0:
                continue
            if flt(job_card.total_completed_qty) <= 0:
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
        for name in frappe.get_all(
            "Master Job Card",
            filters={"previous_opration_master_job_card": self.name},
            pluck="name",
        ):
            frappe.db.set_value(
                "Master Job Card",
                name,
                "previous_opration_master_job_card",
                None,
                update_modified=False,
            )

    def link_job_cards(self):
        """Take up the Job Cards the Work Order already raised for this operation.

        ERPNext creates them when the Work Order is submitted, one per operation row,
        so this card claims the ones matching its own item and operation rather than
        raising any of its own."""
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
            )
            if not job_card:
                missing.append(row)
                continue

            row.db_set("job_card_number", job_card, update_modified=False)
            frappe.db.set_value(
                "Job Card", job_card, "master_job_card", self.name, update_modified=False
            )

        if missing:
            frappe.throw(
                ("The Work Order has no Job Card left for {0}:<br><br>{1}<br><br>"
                 "Every Job Card of this operation is already held by another Master "
                 "Job Card.").format(
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

    def release_job_cards(self):
        """Hand the Job Cards back. They belong to the Work Order, not to this card,
        so cancelling here only releases them -- a submitted one is cancelled first,
        since the qty it reported was reported through this card."""
        for row in (self.get("job_card_detail") or []):
            if not row.job_card_number:
                continue
            if not frappe.db.exists("Job Card", row.job_card_number):
                continue

            job_card = frappe.get_doc("Job Card", row.job_card_number)
            if job_card.docstatus == 1:
                job_card.cancel()

            frappe.db.set_value(
                "Job Card", job_card.name, "master_job_card", None, update_modified=False
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
        # Both, and not set_stock_entry_type(): that reads purpose to work out the
        # type, and ERPNext only fills purpose in from the type after it has already
        # decided which warehouse is mandatory.
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

    @frappe.whitelist()
    def make_material_transfer_for_manufacture(self):
        """Build ONE consolidated 'Material Transfer for Manufacture' Stock Entry
        (Source -> WIP) covering the pending required items of all the linked Job
        Cards of this operation. Returned unsaved so the user can review/submit."""
        if not self.source_warehouse or not self.wip_warehouse:
            frappe.throw(("Source and WIP warehouses are required for the transfer."))

        from erpnext.stock.get_item_details import get_conversion_factor

        stock_entry = frappe.new_doc("Stock Entry")
        stock_entry.stock_entry_type = "Material Transfer for Manufacture"
        stock_entry.purpose = "Material Transfer for Manufacture"
        stock_entry.company = self.company
        stock_entry.from_warehouse = self.source_warehouse
        stock_entry.to_warehouse = self.wip_warehouse
        stock_entry.master_job_card = self.name

        for row in self.required_item:
            pending = flt(row.pending_transfer_qty)
            if pending <= 0:
                continue

            stock_uom = frappe.db.get_value("Item", row.item_code, "stock_uom")
            uom = row.uom or stock_uom
            conversion_factor = (
                flt(get_conversion_factor(row.item_code, uom).get("conversion_factor")) or 1.0
            )

            stock_entry.append("items", {
                "item_code": row.item_code,
                "qty": pending,
                "uom": uom,
                "stock_uom": stock_uom,
                "conversion_factor": conversion_factor,
                "s_warehouse": self.source_warehouse,
                "t_warehouse": self.wip_warehouse,
            })

        if not stock_entry.get("items"):
            frappe.throw(("There is nothing pending to transfer."))

        return stock_entry

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
        self.start_operators(employees)
        self.drive_job_cards("start")
        self.set_card_status("Work In Progress")

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
        written with only an employee and a start time."""
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
        self.close_open_time_logs()
        self.db_set("hold_reason", reason)
        self.set_card_status("On Hold")

    @frappe.whitelist()
    def resume_jobs(self):
        self.drive_job_cards("resume")
        self.open_operator_time_logs()
        self.set_card_status("Work In Progress")

    @frappe.whitelist()
    def complete_jobs(self, rows=None):
        """Report the completed qty, then submit only if the Job Cards allow it.

        ERPNext keeps those two apart: validate_transfer_qty() refuses to submit a Job
        Card carrying its own material rows until that material has reached WIP. That
        is the Material Transfer On = Job Card case -- work can be reported as done
        while the card waits in draft for the transfer. So the operation is completed
        here, and submitted only when nothing is holding it back."""
        self.validate_complete_qty(rows)
        self.book_reported_qty(rows)
        self.drive_job_cards("complete")
        self.close_open_time_logs()

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

        tolerance = 0.001
        detail = {
            row.job_card_number: row
            for row in (self.get("job_card_detail") or [])
            if row.job_card_number
        }

        for data in rows:
            row = detail.get(data.get("job_card_number"))
            if not row:
                continue

            ordered = flt(row.qty_to_manufacture)
            if not ordered:
                continue

            total = (
                flt(row.completed_qty)
                + flt(data.get("completed_qty"))
                + flt(data.get("process_loss_qty"))
                + flt(data.get("pending_qty"))
            )

            if abs(total - ordered) <= tolerance:
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
        """Write the qty reported in a dialog onto the open time logs and close them.

        Onto the time logs rather than straight onto the detail rows, because
        calculate_detail_rows() derives the detail qty from the logs -- a value written
        to the detail row would simply be overwritten on the next save.

        Shared by Pause and Complete: an operator stopping mid-run reports what has
        been made so far in exactly the same terms as one finishing the operation, so
        the progress is not lost until the very end."""
        rows = frappe.parse_json(rows) if rows else []
        if not rows:
            return

        by_job_card = {
            row.get("job_card_number"): row for row in rows if row.get("job_card_number")
        }
        if not by_job_card:
            return

        now = frappe.utils.now()
        # The qty is reported once per Job Card, but a Job Card can have several open
        # logs -- one per operator. Booking it on each would multiply it, since
        # calculate_detail_rows() sums the logs, so only the first carries it.
        booked = set()

        for log in self.time_log:
            data = by_job_card.get(log.job_card_number)
            if not data or log.to_time:
                continue

            if log.job_card_number in booked:
                log.completed_qty = 0.0
                log.rejected_qty = 0.0
            else:
                log.completed_qty = flt(data.get("completed_qty"))
                log.rejected_qty = flt(data.get("rejected_qty"))
                booked.add(log.job_card_number)

            log.to_time = now

        # Process loss lives on the detail row, not the log. Added to what is there
        # rather than replacing it, because the dialog reports this run only -- and
        # left alone when the key is absent, since a Pause does not report it.
        for row in (self.get("job_card_detail") or []):
            data = by_job_card.get(row.job_card_number) or {}
            if data.get("process_loss_qty") is not None:
                row.process_loss_qty = flt(row.process_loss_qty) + flt(
                    data.get("process_loss_qty")
                )

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
        # This card keeps being updated after submit (time logs, totals), so skip
        # the "cannot change after submit" guard and let before_save refresh totals.
        self.flags.ignore_validate_update_after_submit = True
        self.save()

    def drive_job_cards(self, action):
        details = [r for r in (self.get("job_card_detail") or []) if r.job_card_number]
        if not details:
            frappe.throw(("No linked Job Cards to process."))

        employees = [{"employee": e.employee} for e in (self.get("employee") or []) if e.employee]
        if action == "start" and not employees:
            frappe.throw(("Assign at least one Employee before starting."))

        now = frappe.utils.now()
        processed = 0

        for detail in details:
            job_card = frappe.get_doc("Job Card", detail.job_card_number)
            # ERPNext allows start/pause/resume/complete only on draft Job Cards.
            if job_card.docstatus != 0:
                continue

            if action == "start":
                # The operators are already on the card -- sync_employees_to_job_cards()
                # put them there when this card was saved a moment ago.
                job_card.start_timer(start_time=now, employees=employees)
            elif action == "pause":
                job_card.pause_job(end_time=now)
            elif action == "resume":
                job_card.resume_job(start_time=now)
            elif action == "complete":
                qty = flt(detail.completed_qty) or flt(job_card.for_quantity)
                process_loss = flt(detail.process_loss_qty)
                # ERPNext's validate_job_card() insists the three balance exactly:
                # completed + process loss + pending == for quantity. The detail row's
                # Pending Qty is already that balance -- calculate_detail_rows() keeps
                # it -- and declaring it is what lets a part completion submit, with
                # the remainder free to move onto a new card.
                pending = max(flt(job_card.for_quantity) - qty - process_loss, 0.0)

                job_card.complete_job_card(
                    end_time=now,
                    qty=qty,
                    process_loss_qty=process_loss,
                    pending_qty=pending,
                )
                # Not submitted here. Reporting the qty and submitting are separate
                # steps -- submit_completed_job_cards() carries them over when this
                # card is submitted, which cannot happen while material is pending.

            processed += 1

        frappe.msgprint(
            ("{0}: applied on {1} Job Card(s).").format(action.title(), processed),
            indicator="green",
            alert=True,
        )
        return processed


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
        self._set_required_items(mwo)
        self._set_scrap_items()

        self.calculate_detail_rows()
        self.calculate_required_items()
        self.calculate_scrap_items()
        self.calculate_totals()

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
        """One row per MWO item (work order) whose BOM includes this operation.

        An operation added to the order by hand is in no BOM at all, so when none of
        them carry it every item runs through it instead."""
        self.set("job_card_detail", [])

        bom_operation = {}
        for item in mwo.items_to_be_manufacture:
            if not item.bom_no:
                continue
            bom_operation[item.name] = next(
                (r for r in self._bom_operation_rows(item.bom_no)
                 if r.operation == self.operation_name),
                None,
            )

        from_bom = any(bom_operation.values())

        for item in mwo.items_to_be_manufacture:
            if not item.bom_no:
                continue

            bom_op = bom_operation.get(item.name)
            if from_bom and not bom_op:
                # This work order does not run through this operation.
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
            # told it was raised for less than has already been made on it.
            qty = max(
                qty,
                flt(row.completed_qty)
                + flt(row.process_loss_qty)
                + flt(row.rejected_qty),
            )

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

    def _set_required_items(self, mwo):
        """Consolidate the required items of every work order (MWO item) whose
        BOM runs through this operation."""
        self.set("required_item", [])

        consolidated = {}
        for item in mwo.items_to_be_manufacture:
            if not item.bom_no:
                continue

            bom = frappe.get_doc("BOM", item.bom_no)
            # Skip work orders whose BOM does not include this operation.
            if not any(op.operation == self.operation_name for op in bom.operations):
                continue

            bom_qty = flt(bom.quantity) or 1.0
            scale = flt(item.qty_to_manufacture) / bom_qty

            for bi in bom.items:
                # Honour operation-wise material if BOM Items are tagged.
                if bi.get("operation") and bi.operation != self.operation_name:
                    continue
                key = (bi.item_code, bi.get("uom"))
                consolidated.setdefault(key, {
                    "item_code": bi.item_code,
                    "item_name": bi.item_name,
                    "uom": bi.get("uom"),
                    "requried_qty": 0.0,
                })
                consolidated[key]["requried_qty"] += flt(bi.qty) * scale

        for data in consolidated.values():
            rate = flt(frappe.db.get_value("Item", data["item_code"], "valuation_rate"))
            self.append("required_item", {
                "item_code": data["item_code"],
                "item_name": data["item_name"],
                "uom": data["uom"],
                "source_warehouse": self.source_warehouse,
                "requried_qty": data["requried_qty"],
                "available_qty": self._source_warehouse_stock(data["item_code"]),
                "rate": rate,
            })

    def _source_warehouse_stock(self, item_code):
        """Available stock of the item in the source warehouse (shortage check)."""
        if not self.source_warehouse:
            return 0.0
        return flt(frappe.db.get_value(
            "Bin",
            {"item_code": item_code, "warehouse": self.source_warehouse},
            "actual_qty",
        ))

    def _set_scrap_items(self):
        self.set("scrap_item", [])
        bom_nos = list({r.bom_no for r in (self.get("job_card_detail") or []) if r.bom_no})
        if not bom_nos:
            return

        scrap_rows = frappe.get_all(
            "BOM Secondary Item",
            filters={"parent": ["in", bom_nos], "parenttype": "BOM", "type": "Scrap"},
            fields=["item_code", "item_name", "uom", "stock_uom", "qty"],
        )
        consolidated = {}
        for s in scrap_rows:
            consolidated.setdefault(s.item_code, {
                "item_code": s.item_code,
                "item_name": s.item_name,
                "uom": s.uom or s.stock_uom,
                "scrap_qty": 0.0,
            })
            consolidated[s.item_code]["scrap_qty"] += flt(s.qty)

        for data in consolidated.values():
            rate = flt(frappe.db.get_value("Item", data["item_code"], "valuation_rate"))
            self.append("scrap_item", {
                "item_code": data["item_code"],
                "item_name": data["item_name"],
                "uom": data["uom"],
                "scrap_qty": data["scrap_qty"],
                "rate": rate,
                "amount": data["scrap_qty"] * rate,
                "scrap_warehouse": self.scrap_warehouse,
            })

    # ------------------------------------------------------------------
    # Auto-calculations
    # ------------------------------------------------------------------
    def recalculate_time_logs(self):
        for log in self.time_log:
            if log.from_time and log.to_time:
                log.time_in_mins = flt(time_diff_in_seconds(log.to_time, log.from_time)) / 60.0
            else:
                log.time_in_mins = 0

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

        for row in (self.get("job_card_detail") or []):
            if row.job_card_number:
                row.actual_time = actual_by_jc.get(row.job_card_number, 0.0)
                if row.job_card_number in completed_by_jc:
                    row.completed_qty = completed_by_jc[row.job_card_number]
                if row.job_card_number in rejected_by_jc:
                    row.rejected_qty = rejected_by_jc[row.job_card_number]

            row.pending_qty = (
                flt(row.qty_to_manufacture)
                - flt(row.completed_qty)
                - flt(row.process_loss_qty)
                - flt(row.rejected_qty)
            )

    def calculate_required_items(self):
        for row in self.required_item:
            row.pending_transfer_qty = (
                flt(row.requried_qty) - flt(row.transfer_qty) + flt(row.return_qty)
            )
            row.amount = flt(row.consumed_qty) * flt(row.rate)

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
        self.total_pending_qty = flt(self.total_qty_to_manufacture) - flt(self.total_completed_qty)
        self.total_standerd_time = sum(flt(r.standerd_time) for r in detail)
        self.total_actual_time = sum(flt(r.time_in_mins) for r in self.time_log)
        self.total_operating_cost = (flt(self.total_actual_time) / 60.0) * flt(self.hour_rate)

    def set_actual_dates(self):
        from_times = [l.from_time for l in self.time_log if l.from_time]
        to_times = [l.to_time for l in self.time_log if l.to_time]
        self.actual_start_date = min(from_times) if from_times else None
        self.actual_end_date = max(to_times) if to_times else None

    def set_status(self):
        # Respect manual / terminal states.
        if self.status == "On Hold":
            return
        if self.docstatus == 2:
            self.status = "Cancelled"
            return
        detail = (self.get("job_card_detail") or [])
        if self.docstatus == 0 and not detail:
            self.status = "Draft"
            return

        if detail and all(r.status == "Completed" for r in detail):
            self.status = "Completed"
        elif any(r.status == "Work In Progress" for r in detail) or self.time_log:
            self.status = "Work In Progress"
        elif any(flt(r.transferred_qty) for r in detail):
            self.status = "Material Transferred"
        else:
            self.status = "Open"

    # ------------------------------------------------------------------
    # Validations
    # ------------------------------------------------------------------
    def validate_operation_is_in_house(self):
        if self.manufacturing_type and self.manufacturing_type != "In-House":
            frappe.throw("Master Job Card is created only for In-House operations.")


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