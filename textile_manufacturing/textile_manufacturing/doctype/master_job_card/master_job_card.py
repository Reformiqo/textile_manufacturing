# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import flt, now_datetime, time_diff_in_seconds


class MasterJobCard(Document):
    def before_validate(self):
        # This card keeps being updated after submit (time logs, qty, status),
        # and before_save recomputes derived totals -- allow those changes.
        if self.docstatus == 1:
            self.flags.ignore_validate_update_after_submit = True

    def validate(self):
        self.validate_operation_is_in_house()
        self.set_actual_dates()
        self.validate_quality_inspection()

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
        self.create_job_cards()
        self.db_set("status", "Open")

    def on_submit(self):
        self.validate_jobs_completed()
        self.db_set("status", "Completed")

    def validate_jobs_completed(self):
        """Submitting means the operation is finished, so every Job Card under it has
        to be finished too -- the same bar ERPNext sets, where a Job Card cannot be
        submitted with nothing completed against it."""
        pending = [
            row.job_card_number
            for row in (self.get("job_card_detail") or [])
            if row.job_card_number
            and frappe.db.get_value("Job Card", row.job_card_number, "docstatus") != 1
        ]
        if not pending:
            return

        frappe.throw(
            ("These Job Cards are not finished yet:<br><br>{0}<br><br>"
             "Use <b>Job &gt; Complete</b> to finish the operation -- that submits "
             "this card for you.").format(
                "<br>".join(
                    frappe.utils.get_link_to_form("Job Card", name) for name in pending
                )
            ),
            title="Operation Not Complete",
        )

    def on_cancel(self):
        self.cancel_job_cards()
        self.db_set("status", "Cancelled")

    def create_job_cards(self):
        for row in (self.get("job_card_detail") or []):
            if row.job_card_number:
                continue  # already created
            if not row.work_order_number:
                frappe.throw(
                    ("Row {0}: Work Order is required to create a Job Card.").format(row.idx)
                )

            job_card = frappe.new_doc("Job Card")
            job_card.work_order = row.work_order_number
            job_card.production_item = row.item_code
            job_card.bom_no = row.bom_no
            job_card.operation = self.operation_name
            # Without this the card is orphaned from the Work Order's operation row:
            # Job Card.update_work_order() bails on `elif self.operation_id`, so the
            # operation's completed qty stays 0 and a later Manufacture entry fails
            # check_if_operations_completed() with "Job Card not found".
            job_card.operation_id = frappe.db.get_value(
                "Work Order Operation",
                {"parent": row.work_order_number, "operation": self.operation_name},
                "name",
            )
            job_card.workstation_type = self.workstation_type
            job_card.workstation = row.workstation or self.workstation
            job_card.for_quantity = row.qty_to_manufacture
            job_card.wip_warehouse = self.wip_warehouse
            job_card.company = self.company
            job_card.posting_date = frappe.utils.nowdate()
            job_card.insert()

            row.db_set("job_card_number", job_card.name, update_modified=False)

    def cancel_job_cards(self):
        """Cancel/remove the Job Cards created from this Master Job Card."""
        for row in (self.get("job_card_detail") or []):
            if not row.job_card_number:
                continue
            if not frappe.db.exists("Job Card", row.job_card_number):
                continue

            job_card = frappe.get_doc("Job Card", row.job_card_number)
            if job_card.docstatus == 1:
                job_card.cancel()

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

        stock_entry.set_stock_entry_type()
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
        self.db_set("status", "Work In Progress")

    def start_operators(self, employees):
        if isinstance(employees, str):
            employees = frappe.parse_json(employees)
        employees = employees or []

        now = frappe.utils.now()
        existing = {e.employee for e in (self.get("employee") or [])}

        for emp in employees:
            emp_id = (emp.get("employee") or emp.get("name")) if isinstance(emp, dict) else emp
            if not emp_id:
                continue

            if emp_id not in existing:
                self.append("employee", {"employee": emp_id})
                existing.add(emp_id)


            self.append("time_log", {"employee": emp_id, "from_time": now})

        self.save_after_submit()

    @frappe.whitelist()
    def pause_jobs(self, reason=None):
        self.drive_job_cards("pause")
        self.close_open_time_logs()
        self.db_set("hold_reason", reason)
        self.db_set("status", "On Hold")

    @frappe.whitelist()
    def resume_jobs(self):
        self.drive_job_cards("resume")
        self.open_operator_time_logs()
        self.db_set("status", "Work In Progress")

    @frappe.whitelist()
    def complete_jobs(self):
        self.drive_job_cards("complete")
        self.close_open_time_logs()
        self.submit()

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
        """Open a fresh Time Log row (from_time = now) for each assigned operator."""
        now = frappe.utils.now()
        for emp in (self.get("employee") or []):
            if emp.employee:
                self.append("time_log", {"employee": emp.employee, "from_time": now})
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
                # Populate the Job Card's own employees so pause/resume work later.
                job_card.set("employee", [{"employee": e["employee"]} for e in employees])
                job_card.start_timer(start_time=now, employees=employees)
            elif action == "pause":
                job_card.pause_job(end_time=now)
            elif action == "resume":
                job_card.resume_job(start_time=now)
            elif action == "complete":
                qty = flt(detail.completed_qty) or flt(job_card.for_quantity)
                job_card.complete_job_card(
                    end_time=now,
                    qty=qty,
                    process_loss_qty=flt(detail.process_loss_qty),
                )
                job_card.reload()
                job_card.submit()

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
        """One row per MWO item (work order) whose BOM includes this operation."""
        self.set("job_card_detail", [])

        for item in mwo.items_to_be_manufacture:
            if not item.bom_no:
                continue

            bom_op = next(
                (r for r in self._bom_operation_rows(item.bom_no)
                 if r.operation == self.operation_name),
                None,
            )
            if not bom_op:
                # This work order does not run through this operation.
                continue

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
                "workstation": bom_op.workstation or self.workstation,
                "qty_to_manufacture": item.qty_to_manufacture,
                "standerd_time": bom_op.time_in_mins,
                "status": "Open",
            })

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