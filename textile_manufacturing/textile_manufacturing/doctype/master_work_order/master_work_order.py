# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import cint, flt


class MasterWorkOrder(Document):
    def onload(self):
        # Settled here so the form knows whether to offer the button by the time it
        # draws, the way the Work Order settles Create Job Card in its own onload.
        # The quantities travel with the flag: the operation rows carry a Pending Qty
        # of their own, but only from the moment a card last reported against them,
        # and the dialog should not open empty on an order that predates that.
        pending = self.pending_master_job_card_operations()
        self.set_onload("show_pending_master_job_card_button", bool(pending))
        self.set_onload("pending_master_job_card_operations", pending)

        # The Finish dialog's rows, and the ceiling on each: what the last operation
        # turned out, which the form itself does not carry. The reason travels with
        # them, for when the order has work left but the line has not turned it out.
        rows = self.pending_manufacture_rows()
        self.set_onload("pending_manufacture_rows", rows)
        self.set_onload(
            "finish_blocked_reason",
            self.finish_blocked_reason()
            if rows and not any(flt(row["qty"]) > 0 for row in rows)
            else None,
        )

    def validate(self):
        self.validate_unique_production_plan()

        for row in self.items_to_be_manufacture:
            if not row.qty_to_manufacture:
                frappe.throw(
                    ("Row {0}: Qty to Manufacture is not set for item {1}").format(
                        row.idx, row.item_code
                    )
                )


    def on_submit(self):
        self.create_work_orders()
        self.create_master_job_cards()

        status = "Not Started" if not self.skip_material_transfer_to_wip_warehouse else "In Process"
        self.db_set("status", status)

    def before_update_after_submit(self):
        self.validate_manufacturing_type_not_rerouted()

    def on_update_after_submit(self):
        self.propagate_new_operations()

    def before_cancel(self):
        self.validate_linked_docs_cancelled()

    def on_cancel(self):
        self.db_set("status", "Cancelled")

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def validate_unique_production_plan(self):
        if not self.production_plan_number:
            return

        existing = frappe.get_all(
            "Master Work Order",
            filters={
                "production_plan_number": self.production_plan_number,
                "docstatus": ["<", 2],
                "name": ["!=", self.name],
            },
            pluck="name",
        )
        if not existing:
            return

        frappe.throw(
            ("Master Work Order {0} already exists for Production Plan {1}.<br><br>"
             "Cancel it before raising another.").format(
                frappe.utils.get_link_to_form("Master Work Order", existing[0]),
                self.production_plan_number,
            ),
            title="Master Work Order Already Exists",
        )

    def validate_manufacturing_type_not_rerouted(self):
        was = dict(frappe.get_all(
            "Master Work Order Operation",
            filters={"parent": self.name, "parenttype": "Master Work Order"},
            fields=["name", "manufacturing_type"],
            as_list=True,
        ))

        for row in self.operations:
            previous = was.get(row.name)
            if not previous or row.manufacturing_type == previous:
                continue

            frappe.throw(
                ("Row {0}: {1} is already running as {2}, so its Manufacturing Type "
                 "cannot be changed to {3}.<br><br>The work has been raised against "
                 "that choice -- cancel this order to route the operation "
                 "differently.").format(
                    row.idx,
                    frappe.bold(row.opration_name or ""),
                    frappe.bold(previous),
                    frappe.bold(row.manufacturing_type or "empty"),
                ),
                title="Operation Already Routed",
            )

    def validate_linked_docs_cancelled(self):
        pending = []

        for row in self.items_to_be_manufacture:
            if row.work_order_number and frappe.db.get_value(
                "Work Order", row.work_order_number, "docstatus"
            ) == 1:
                pending.append(("Work Order {0}").format(row.work_order_number))

    # ------------------------------------------------------------------
    # Create Work Orders
    # ------------------------------------------------------------------
    def create_work_orders(self):
        for row in self.items_to_be_manufacture:
            work_order = frappe.new_doc("Work Order")
            work_order.company = self.company
            work_order.project = self.get("project")
            work_order.production_item = row.item_code
            work_order.bom_no = row.bom_no
            work_order.qty = row.qty_to_manufacture
            work_order.source_warehouse = row.source_warehouse
            work_order.wip_warehouse = row.wip_warehouse
            work_order.fg_warehouse = row.fg_warehouse
            work_order.scrap_warehouse = row.scrap_warehouse
            work_order.use_multi_level_bom = self.use_multi_level_bom
            work_order.skip_transfer = self.skip_material_transfer_to_wip_warehouse
            work_order.planned_start_date = self.posting_date or frappe.utils.now_datetime()

            self.set_in_house_operations(work_order)

            work_order.insert()
            work_order.submit()   # This will trigger Job Card Creation

            row.db_set({
                "work_order_number": work_order.name,
                "pending_qty": flt(row.qty_to_manufacture) - flt(row.manufacture_qty),
            }, update_modified=False)


    def set_in_house_operations(self, work_order):
        if not work_order.bom_no:
            return

        work_order.set_work_order_operations()

        in_house = {
            op.opration_name
            for op in self.operations
            if op.manufacturing_type == "In-House" and op.opration_name
        }

        work_order.operations = [
            op for op in work_order.operations if op.operation in in_house
        ]


    # ------------------------------------------------------------------
    # Fetch From Production Plan Details
    # ------------------------------------------------------------------
    @frappe.whitelist()
    def fetch_from_production_plan(self):
        if not self.production_plan_number:
            return

        production_plan = frappe.get_doc(
            "Production Plan", self.production_plan_number
        )

        self.posting_date = frappe.utils.now_datetime()
        self.company = production_plan.company
        self.cost_center = frappe.db.get_value("Company", self.company, "cost_center")

        set_default_warehouses(self)

        self.fetch_production_plan_items(production_plan)

        bom_ids = list(
            {row.bom_no for row in self.items_to_be_manufacture if row.bom_no}
        )

        operations = self.get_operations(bom_ids)
        self.set_operations(operations)

        items = self.get_required_items(bom_ids)
        self.set_required_items(items)

        scrap_items = self.get_scrap_items(bom_ids)
        self.set_scrap_items(scrap_items)

        self.set_planned_dates()


    def _make_master_work_order(self):
        self.fetch_from_production_plan()
        self.insert()
        return self.name

    def fetch_production_plan_items(self, production_plan):
        self.set("items_to_be_manufacture", [])

        total_items = 0
        for item in production_plan.po_items:
            total_items += item.planned_qty
            self.append(
                "items_to_be_manufacture",
                {
                    "item_code": item.item_code,
                    "uom": item.stock_uom,
                    "production_plan_number": self.production_plan_number,
                    "qty_to_manufacture": item.planned_qty,
                    "manufacture_qty" : item.produced_qty,
                    "pending_qty": flt(item.planned_qty) - flt(item.produced_qty),
                    "bom_no": item.bom_no,
                    "source_warehouse": self.source_warehouse,
                    "fg_warehouse": self.fg_warehouse,
                    "wip_warehouse": self.wip_warehouse,
                    "scrap_warehouse": self.scrap_warehouse,
                    "sales_order_number" : item.sales_order,
                    "planned_start_date" : item.planned_start_date
                },
            )

        self.set("total_qty_to_manufacture", total_items)


    def get_operations(self, bom_ids=None):
        if not bom_ids:
            return []

        operations = frappe.get_all(
            "BOM Operation",
            filters={
                "parent": ["in", bom_ids],
                "parenttype": "BOM",
            },
            fields=[
                "parent",
                "sequence_id",
                "operation",
                "workstation",
                "workstation_type",
                "time_in_mins",
            ],
            order_by="parent",
        )

        return operations


    def set_operations(self, operations):
        self.set("operations", [])

        # Total qty to manufacture per BOM (multiple items can share a BOM).
        bom_qty_map = {}
        for item in self.items_to_be_manufacture:
            if item.bom_no:
                bom_qty_map[item.bom_no] = bom_qty_map.get(item.bom_no, 0) + (item.qty_to_manufacture or 0)

        # One row per operation, even if several BOMs run it.
        consolidated = {}
        unique_workstation = set()
        for op in operations:
            unique_workstation.add(op.workstation)
            row = consolidated.setdefault(op.operation, {
                "opration_sequence_no": op.sequence_id,
                "opration_name": op.operation,
                "workstation": op.workstation,
                "workstation_type": op.workstation_type,
                "standerd_time": 0,
                "total_qty_to_manufacture": 0,
                "counted_boms": set(),
            })
            row["standerd_time"] += op.time_in_mins or 0
            # Add each BOM's qty only once per operation.
            if op.parent not in row["counted_boms"]:
                row["counted_boms"].add(op.parent)
                row["total_qty_to_manufacture"] += bom_qty_map.get(op.parent, 0)

        hour_rate_map = frappe._dict(frappe.get_all("Workstation", {"name": ["in", list(unique_workstation)]}, ["name", "hour_rate"], as_list=1))

        for data in sorted(consolidated.values(), key=lambda d: d["opration_sequence_no"] or 0):
            data.pop("counted_boms", None)
            data["hour_rate"] = hour_rate_map.get(data["workstation"])
            self.append("operations", data)


    def get_required_items(self, bom_ids):
        items = frappe.get_all(
            "BOM Item",
            filters={
                "parent": ["in", bom_ids],
                "parenttype": "BOM",
            },
            fields=[
                "parent",
                "item_code",
                "item_name",
                "uom",
                "qty",
            ],
        )
        return items


    def set_required_items(self, items):
        self.set("required_items", [])

        bom_qty_map = {}
        for item in self.items_to_be_manufacture:
            if item.bom_no:
                bom_qty_map[item.bom_no] = bom_qty_map.get(item.bom_no, 0) + flt(item.qty_to_manufacture)

        bom_base_qty = {}
        if bom_qty_map:
            bom_base_qty = frappe._dict(
                frappe.get_all(
                    "BOM", {"name": ["in", list(bom_qty_map)]}, ["name", "quantity"], as_list=1
                )
            )

        # One row per raw material, totalled across the BOMs that need it.
        consolidated = {}
        for item in items:
            row = consolidated.setdefault(item.item_code, {
                "item_code": item.item_code,
                "item_name": item.item_name,
                "uom": item.uom,
                "requried_qty": 0,
            })
            scale = flt(bom_qty_map.get(item.parent)) / (flt(bom_base_qty.get(item.parent)) or 1.0)
            row["requried_qty"] += flt(item.qty) * scale

        items_details = frappe._dict(frappe.get_all("Item", {"name": ["in", list(consolidated)]}, ["name", "valuation_rate"], as_list=1))

        for data in consolidated.values():
            rate = items_details.get(data["item_code"], 0) or 0
            available_qty = self.get_source_warehouse_stock(data["item_code"])
            self.append("required_items", {
                "item_code": data["item_code"],
                "item_name": data["item_name"],
                "uom": data["uom"],
                "source_warehouse": self.source_warehouse,
                "available_qty": available_qty,
                "requried_qty": data["requried_qty"],
                "transfer_qty": 0,
                "return_qty": 0,
                "consumed_qty": 0,
                "rate": rate,
                "amount": data["requried_qty"] * rate,
            })

    def get_source_warehouse_stock(self, item_code):
        if not self.source_warehouse:
            return 0

        return frappe.utils.flt(frappe.db.get_value(
            "Bin",
            {"item_code": item_code, "warehouse": self.source_warehouse},
            "actual_qty",
        ))

    def get_scrap_items(self, bom_ids):
        items = frappe.get_all(
            "BOM Secondary Item",
            filters={
                "parent": ["in", bom_ids],
                "parenttype": "BOM",
                "type" : "Scrap"
            },
            fields=[
                "parent",
                "item_code",
                "item_name",
                "uom",
                "qty",

            ],
        )
        return items

    def set_scrap_items(self, scrap_items):
        self.set("scrap_item", [])

        # One row per scrap item, totalled across the BOMs that produce it.
        consolidated = {}
        for item in scrap_items:
            row = consolidated.setdefault(item.item_code, {
                "item_code": item.item_code,
                "item_name": item.item_name,
                "uom": item.uom,
                "scrap_qty": 0,
            })
            row["scrap_qty"] += item.qty or 0

        for data in consolidated.values():
            self.append("scrap_item", {
                "item_code": data["item_code"],
                "item_name": data["item_name"],
                "uom": data["uom"],
                "scrap_qty": data["scrap_qty"],
                "scrap_warehouse": self.scrap_warehouse,
            })

    def set_planned_dates(self):
        if not self.items_to_be_manufacture:
            return

        start = self.items_to_be_manufacture[0].planned_start_date
        if not start:
            return

        self.planned_start_date = start

        total_minutes = sum(frappe.utils.flt(op.standerd_time) for op in self.operations)
        self.planned_end_date = frappe.utils.add_to_date(start, minutes=total_minutes)

    # -------------------------------------------------
    # Master Work Order Status
    # -------------------------------------------------

    @frappe.whitelist()
    def start_material_transfer(self, rows=None, materials=None):
        self.transfer_material_for_work_orders(rows, materials)
        if not self.actual_start_date:
            self.db_set("actual_start_date", frappe.utils.now_datetime())


    def can_transfer_material(self):
        if self.skip_material_transfer_to_wip_warehouse:
            return False
        if self.material_transfer_on == "Job Card":
            return False
        if not self.wip_warehouse:
            return False
        return True

    def pending_transfer_by_work_order(self):
        """Read from the database, not from self -- an old tab posts a stale
        Material Transfer Qty and would ask for more than is left."""
        pending = {}
        for row in frappe.get_all(
            "Master Work Order Item",
            filters={"parent": self.name, "parenttype": "Master Work Order"},
            fields=["work_order_number", "qty_to_manufacture", "mateial_transfer_qty"],
        ):
            if not row.work_order_number:
                continue

            qty = flt(row.qty_to_manufacture) - flt(row.mateial_transfer_qty)
            if qty <= 0:
                continue

            pending[row.work_order_number] = qty

        return pending

    def validated_transfer_rows(self, rows=None):
        """The {work order, qty} rows a transfer may go ahead with."""
        allowed = self.pending_transfer_by_work_order()
        if not allowed:
            frappe.throw(
                "There is nothing left to transfer to the WIP Warehouse.<br><br>"
                "Refresh the Master Work Order -- material has been transferred "
                "against it since this form was opened.",
                title="Nothing Left to Transfer",
            )

        rows = frappe.parse_json(rows) if rows else [
            {"work_order_number": work_order, "qty": qty}
            for work_order, qty in allowed.items()
        ]

        validated = []
        for row in rows:
            work_order = row.get("work_order_number")
            qty = flt(row.get("qty"))
            if not work_order or qty <= 0:
                continue
            if work_order not in allowed:
                frappe.throw(
                    ("{0}: nothing is left to transfer against Work Order {1}.<br><br>"
                     "Refresh the Master Work Order -- material has been transferred "
                     "against it since this form was opened.").format(
                        row.get("item_code") or work_order, work_order
                    ),
                    title="Nothing Left to Transfer",
                )

            if qty > allowed[work_order]:
                frappe.throw(
                    ("{0}: only {1} is left to transfer, but {2} was asked for.<br><br>"
                     "Refresh the Master Work Order -- material has been transferred "
                     "against it since this form was opened.").format(
                        row.get("item_code") or work_order,
                        flt(allowed[work_order], 3),
                        flt(qty, 3),
                    ),
                    title="Qty to Transfer Too High",
                )

            status = frappe.db.get_value("Work Order", work_order, "status")
            if status in ("Completed", "Closed", "Stopped", "Cancelled"):
                frappe.throw(
                    ("Work Order {0} is {1} -- material cannot be transferred to it.").format(
                        work_order, status
                    )
                )

            validated.append({"work_order_number": work_order, "qty": qty})

        return validated

    def draft_transfer_entry(self, work_order, qty):
        """The Stock Entry ERPNext would raise for this qty, unsaved."""
        from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry

        return frappe.get_doc(
            make_stock_entry(work_order, "Material Transfer for Manufacture", qty)
        )

    @frappe.whitelist()
    def get_transfer_materials(self, rows=None):
        """The raw material the transfer would move, for review before it happens.
        Nothing is saved here -- the draft is built only to read its items off."""
        if not self.can_transfer_material():
            return []

        materials = []
        for row in self.validated_transfer_rows(rows):
            stock_entry = self.draft_transfer_entry(row["work_order_number"], row["qty"])

            for item in stock_entry.items:
                materials.append({
                    "work_order_number": row["work_order_number"],
                    "row_id": item.idx,
                    "item_code": item.item_code,
                    "item_name": item.item_name,
                    "s_warehouse": item.s_warehouse,
                    "t_warehouse": item.t_warehouse,
                    "uom": item.uom,
                    "suggested_qty": flt(item.qty),
                    "qty": flt(item.qty),
                    "available_qty": flt(item.get("actual_qty")),
                })

        return materials

    def transfer_material_for_work_orders(self, rows=None, materials=None):
        if not self.can_transfer_material():
            return

        rows = self.validated_transfer_rows(rows)
        edited = self.materials_by_work_order(materials)

        for row in rows:
            work_order = row["work_order_number"]
            stock_entry = self.draft_transfer_entry(work_order, row["qty"])
            self.apply_edited_materials(stock_entry, edited.get(work_order))

            stock_entry.master_work_order = self.name
            if not stock_entry.get("project"):
                stock_entry.project = self.get("project")
            stock_entry.insert()
            stock_entry.submit()

        self.update_transferred_qty()

    def materials_by_work_order(self, materials):
        """Edited quantities keyed by work order, then by the row they came from."""
        by_work_order = {}

        for row in (frappe.parse_json(materials) if materials else []):
            work_order = row.get("work_order_number")
            if not work_order:
                continue
            by_work_order.setdefault(work_order, {})[
                cint(row.get("row_id"))
            ] = flt(row.get("qty"))

        return by_work_order

    def apply_edited_materials(self, stock_entry, edited):
        """Carry the reviewed quantities onto the draft.

        Only the qty is touched -- warehouses, rates and conversion factors stay as
        ERPNext worked them out. A row set to zero is dropped from the entry."""
        if not edited:
            return

        items = []
        for item in stock_entry.items:
            if item.idx in edited:
                item.qty = edited[item.idx]

            if flt(item.qty) > 0:
                items.append(item)

        if not items:
            frappe.throw(
                ("Every raw material line for Work Order {0} was set to zero -- "
                 "there is nothing to transfer.").format(stock_entry.work_order),
                title="Nothing to Transfer",
            )

        stock_entry.items = items

    def update_transferred_qty(self):
        for row in self.items_to_be_manufacture:
            if not row.work_order_number:
                continue

            row.db_set(
                "mateial_transfer_qty",
                flt(
                    frappe.db.get_value(
                        "Work Order",
                        row.work_order_number,
                        "material_transferred_for_manufacturing",
                    )
                ),
                update_modified=False,
            )

        self.update_required_item_transfers()
        self.set_status_from_work_orders()

    def update_required_item_transfers(self):
        work_orders = self.linked_work_orders()
        if not work_orders:
            return

        totals = {}
        for item in frappe.get_all(
            "Work Order Item",
            filters={"parent": ["in", work_orders], "parenttype": "Work Order"},
            fields=["item_code", "transferred_qty", "consumed_qty", "returned_qty"],
        ):
            row = totals.setdefault(
                item.item_code,
                {"transfer_qty": 0.0, "consumed_qty": 0.0, "return_qty": 0.0},
            )
            row["transfer_qty"] += flt(item.transferred_qty)
            row["consumed_qty"] += flt(item.consumed_qty)
            row["return_qty"] += flt(item.returned_qty)

        for row in self.required_items:
            data = totals.get(row.item_code) or {}
            transfer_qty = flt(data.get("transfer_qty"))
            return_qty = flt(data.get("return_qty"))
            row.db_set({
                "transfer_qty": transfer_qty,
                "consumed_qty": flt(data.get("consumed_qty")),
                "return_qty": return_qty,
                "pending_transfer_qty": flt(row.requried_qty) - transfer_qty + return_qty,
            }, update_modified=False)

    # -----------------------------------
    # Operations added after the order is raised
    # -----------------------------------
    def propagate_new_operations(self):
        # This is used to add operations after the master work order is submitted
        raised = {card.operation_name for card in self.master_job_cards()}

        for op in self.operations:
            if op.manufacturing_type != "In-House":
                continue
            if not op.opration_name or op.opration_name in raised:
                continue

            for work_order in self.linked_work_orders():
                self.add_work_order_operation(work_order, op)

            card = self.make_master_job_card_for(op.opration_name)
            raised.add(op.opration_name)

            frappe.msgprint(
                ("Operation {0} added, and Master Job Card {1} raised for it.").format(
                    frappe.bold(op.opration_name),
                    frappe.utils.get_link_to_form("Master Job Card", card),
                ),
                indicator="green",
            )

    def add_work_order_operation(self, work_order, operation):
        """Add the operation to a submitted Work Order, and raise its Job Card."""
        from erpnext.manufacturing.doctype.work_order.work_order import create_job_card

        work_order = frappe.get_doc("Work Order", work_order)
        if work_order.docstatus != 1:
            return
        if any(op.operation == operation.opration_name for op in work_order.operations):
            return
        # if this operation is not in bom then we dont need to link this bom
        if not frappe.db.exists(
            "BOM Operation",
            {"parent": work_order.bom_no, "operation": operation.opration_name}
        ):
            return

        row = work_order.append("operations", {
            "operation": operation.opration_name,
            "workstation": operation.workstation,
            "workstation_type": operation.workstation_type,
            "sequence_id": operation.opration_sequence_no,
            "time_in_mins": flt(operation.standerd_time),
            "hour_rate": flt(operation.hour_rate),
            "status": "Pending",
            "completed_qty": 0,
            "process_loss_qty": 0,
        })
        row.docstatus = work_order.docstatus
        row.db_insert()

        # Not a stored field -- ERPNext sets it on the row in
        # split_qty_based_on_batch_size() before it reaches create_job_card(), and
        # the whole order goes on one card here.
        row.job_card_qty = flt(work_order.qty)
        create_job_card(work_order, row, auto_create=True)

    def make_master_job_card_for(self, operation):
        """A card for one operation, chained onto the last one raised here."""
        previous = frappe.db.get_value(
            "Master Job Card",
            {"master_work_order_number": self.name, "docstatus": ["<", 2]},
            "name",
            order_by="creation desc",
        )

        master_job_card = frappe.new_doc("Master Job Card")
        master_job_card.master_work_order_number = self.name
        master_job_card.operation_name = operation
        master_job_card.previous_opration_master_job_card = previous
        master_job_card.fetch_from_master_work_order()
        master_job_card.insert()

        return master_job_card.name

    # -----------------------------------
    # Create Master Job Card
    # -----------------------------------
    def create_master_job_cards(self):
        created = []

        previous_master_job_card = None
        for op in self.operations:
            if op.manufacturing_type != "In-House":
                continue

            master_job_card = frappe.new_doc("Master Job Card")
            master_job_card.master_work_order_number = self.name
            master_job_card.operation_name = op.opration_name
            master_job_card.previous_opration_master_job_card = previous_master_job_card
            master_job_card.fetch_from_master_work_order()
            master_job_card.insert()

            previous_master_job_card = master_job_card.name
            created.append(master_job_card.name)

        if created:
            frappe.msgprint(
                ("Created {0} Master Job Card(s): {1}").format(
                    len(created), ", ".join(created)
                ),
                indicator="green",
                alert=True,
            )

        return created

    # ------------------------------------------------------------------
    # Finish -- produce the finished goods
    # ------------------------------------------------------------------
    def work_order_process_loss(self):
        """The process loss a Manufacture entry will take off, and the loss already
        booked, per Work Order.

        The Stock Entry's set_process_loss_qty() reads the highest process loss on any
        of the Work Order's operation rows, and load_items_from_bom() then raises the
        finished good for fg_completed_qty less that figure. So a Manufacture entry has
        to be raised for the pieces put through the line, not the good ones that came
        off it -- asking for 5 where 5 were also lost leaves 5 - 5 = 0 finished goods,
        and ERPNext refuses the entry for having none.

        Already booked is the Work Order's own process_loss_qty, which it keeps as the
        sum over its submitted Manufacture entries. Without it the loss would look
        outstanding for ever and the Finish would keep offering it."""
        work_orders = self.linked_work_orders()
        if not work_orders:
            return {}, {}

        to_deduct = {}
        for row in frappe.get_all(
            "Work Order Operation",
            filters={"parent": ["in", work_orders], "parenttype": "Work Order"},
            fields=["parent", "process_loss_qty"],
        ):
            to_deduct[row.parent] = max(
                to_deduct.get(row.parent, 0.0), flt(row.process_loss_qty)
            )

        booked = {
            row.name: flt(row.process_loss_qty)
            for row in frappe.get_all(
                "Work Order",
                filters={"name": ["in", work_orders]},
                fields=["name", "process_loss_qty"],
            )
        }

        return to_deduct, booked

    def pending_manufacture_by_work_order(self):
        """Finished goods still to be booked, per Work Order -- good pieces only.

        What the operator is asked for and what they type: nothing can be booked as
        made beyond what came off the last operation. Part production is exactly this
        case: 5 of 10 off the line means 5 to finish, and the other 5 only once a
        pending Master Job Card has run them.

        The process loss is not in this figure. It is added on the way into the Stock
        Entry, in finish_work_orders(), because that is the only place it belongs."""
        output = self.final_operation_output()

        pending = {}
        for row in frappe.get_all(
            "Master Work Order Item",
            filters={"parent": self.name, "parenttype": "Master Work Order"},
            fields=[
                "work_order_number", "qty_to_manufacture",
                "mateial_transfer_qty", "manufacture_qty",
            ],
        ):
            if not row.work_order_number:
                continue

            if self.skip_material_transfer_to_wip_warehouse:
                ceiling = flt(row.qty_to_manufacture)
            else:
                ceiling = flt(row.mateial_transfer_qty)

            if row.work_order_number in output:
                ceiling = min(ceiling, output[row.work_order_number])

            qty = ceiling - flt(row.manufacture_qty)
            if qty <= 0:
                continue

            pending[row.work_order_number] = qty

        return pending

    def hold_process_loss_to_actual(self, work_orders):
        """Hold each Work Order's process loss to what was really lost.

        ERPNext totals it over the Manufacture entries, and every one of those carries
        the same figure -- set_process_loss_qty() stamps each entry with the highest
        process loss on any operation row, taking no account of what earlier entries
        already booked. With one entry that is right. Part production makes several,
        and the same loss would then be counted once per entry, pushing produced plus
        loss past the quantity ordered."""
        to_deduct, _booked = self.work_order_process_loss()

        for work_order in set(work_orders):
            actual = flt(to_deduct.get(work_order))
            booked = flt(frappe.db.get_value("Work Order", work_order, "process_loss_qty"))
            if abs(booked - actual) <= 0.001:
                continue

            doc = frappe.get_doc("Work Order", work_order)
            doc.db_set("process_loss_qty", actual)
            doc.db_set("status", doc.get_status())

    def outstanding_manufacture_by_work_order(self):
        """What the order still has to produce, whatever state the line is in.

        Not the same as what may be produced right now -- that is
        pending_manufacture_by_work_order(), which holds the qty to what has actually
        come off the last operation. This figure decides whether the Finish is offered
        at all, so an order with work still to do keeps its button and is told why it
        cannot go ahead, instead of the button quietly disappearing."""
        _to_deduct, booked = self.work_order_process_loss()

        outstanding = {}
        for row in self.items_to_be_manufacture:
            if not row.work_order_number:
                continue

            if self.skip_material_transfer_to_wip_warehouse:
                ceiling = flt(row.qty_to_manufacture)
            else:
                ceiling = flt(row.mateial_transfer_qty)

            qty = (
                ceiling
                - flt(row.manufacture_qty)
                - flt(booked.get(row.work_order_number))
            )
            if qty > 0:
                outstanding[row.work_order_number] = qty

        return outstanding

    def finish_blocked_reason(self):
        """Why the Finish cannot go ahead, when something is still outstanding.

        None when it can. The operations that have not turned anything out yet are
        named, because that is the only thing the operator can do about it."""
        in_house = self.in_house_operations()
        if not in_house:
            return None

        waiting = []
        for card in self.master_job_cards():
            if card.status != "Completed":
                waiting.append("{0} -- {1} is {2}".format(
                    frappe.utils.get_link_to_form("Master Job Card", card.name),
                    frappe.bold(card.operation_name or ""),
                    card.status,
                ))

        if not waiting:
            return (
                "Nothing has come off {0} yet, so there is nothing to book as "
                "finished."
            ).format(frappe.bold(in_house[-1].opration_name or ""))

        return (
            "Nothing can be finished until the operations have run.<br><br>{0}"
            "<br><br>Report the qty on those cards first -- the Finish is held to "
            "what {1} actually turns out."
        ).format("<br>".join(waiting), frappe.bold(in_house[-1].opration_name or ""))

    def pending_manufacture_rows(self):
        """The rows the Finish dialog offers, and the qty each may go up to.

        Offered whenever the order has something left to produce. Qty to Produce is
        held to what the line has turned out, so it can be zero -- the form then says
        why rather than hiding the button."""
        if self.docstatus != 1:
            return []

        outstanding = self.outstanding_manufacture_by_work_order()
        if not outstanding:
            return []

        allowed = self.pending_manufacture_by_work_order()
        to_deduct, _booked = self.work_order_process_loss()

        rows = []
        for row in self.items_to_be_manufacture:
            if row.work_order_number not in outstanding:
                continue

            qty = flt(allowed.get(row.work_order_number))

            rows.append({
                "work_order_number": row.work_order_number,
                "item_code": row.item_code,
                "t_warehouse": row.fg_warehouse or self.fg_warehouse,
                "qty_to_manufacture": flt(row.qty_to_manufacture),
                "transferred_qty": flt(row.mateial_transfer_qty),
                "produced_qty": flt(row.manufacture_qty),
                # Shown beside the qty because the entry is raised for the pieces put
                # through: ask for 5 with 5 lost and 5 finished goods are booked.
                "process_loss_qty": flt(to_deduct.get(row.work_order_number)),
                "outstanding_qty": flt(outstanding[row.work_order_number]),
                "pending_qty": qty,
                "qty": qty,
            })

        return rows

    def master_job_cards(self):
        """The Master Job Cards raised against this order, newest last."""
        return frappe.get_all(
            "Master Job Card",
            filters={"master_work_order_number": self.name, "docstatus": ["<", 2]},
            fields=["name", "operation_name", "status", "docstatus"],
            order_by="creation",
        )

    # ------------------------------------------------------------------
    # Part production -- the balance an operation has still to run
    # ------------------------------------------------------------------
    def in_house_operations(self):
        """In-House operations, in the order they run -- which is the order of the
        rows in the table.

        Not by Opration Sequence No: it comes from the BOM and is 0 on every row of
        plenty of orders, so it cannot be relied on to say which operation runs first."""
        return sorted(
            (op for op in self.operations if op.manufacturing_type == "In-House"),
            key=lambda op: cint(op.idx),
        )

    def operation_detail_rows(self, operation, fields):
        cards = frappe.get_all(
            "Master Job Card",
            filters={
                "master_work_order_number": self.name,
                "operation_name": operation,
                "docstatus": ["<", 2],
            },
            pluck="name",
        )
        if not cards:
            return []

        return frappe.get_all(
            "Master Job Card Detail",
            filters={"parent": ["in", cards], "parenttype": "Master Job Card"},
            fields=fields,
        )

    def operation_balances(self):
        """What each operation has made and lost, per Work Order.

        Summed over every card of the operation -- the one the order was raised with
        and any pending card carrying a balance on from it. Read in one pass over the
        whole order, because onload asks this of every operation each time the form
        opens."""
        cards = frappe.get_all(
            "Master Job Card",
            filters={"master_work_order_number": self.name, "docstatus": ["<", 2]},
            fields=["name", "operation_name"],
        )
        if not cards:
            return {}

        operation_by_card = {card.name: card.operation_name for card in cards}

        balances = {}
        for row in frappe.get_all(
            "Master Job Card Detail",
            filters={
                "parent": ["in", list(operation_by_card)],
                "parenttype": "Master Job Card",
            },
            fields=[
                "parent", "work_order_number",
                "completed_qty", "process_loss_qty", "rejected_qty",
            ],
        ):
            operation = operation_by_card.get(row.parent)
            if not operation or not row.work_order_number:
                continue

            balance = balances.setdefault(operation, {}).setdefault(
                row.work_order_number, {"completed": 0.0, "loss": 0.0}
            )
            balance["completed"] += flt(row.completed_qty)
            balance["loss"] += flt(row.process_loss_qty) + flt(row.rejected_qty)

        return balances

    def pending_by_operation(self):
        """What each operation still has to run, per Work Order.

        Measured against the order's own quantity, which never moves, less what this
        operation made and lost and what was lost before it ever got here. That last
        term is what separates a run that is merely unfinished from one that finished
        short: 5 of 10 made upstream with nothing lost leaves 5 still coming, but 5
        made and 5 lost leaves nothing -- those pieces are gone, and no operation
        downstream can ever work them."""
        ordered = {
            row.work_order_number: flt(row.qty_to_manufacture)
            for row in self.items_to_be_manufacture
            if row.work_order_number
        }
        balances = self.operation_balances()

        pending = {}
        upstream = {}

        for op in self.in_house_operations():
            by_work_order = balances.get(op.opration_name) or {}

            pending[op.opration_name] = {}
            for work_order, balance in by_work_order.items():
                qty = (
                    flt(ordered.get(work_order))
                    - balance["completed"]
                    - balance["loss"]
                    - flt(upstream.get(work_order))
                )
                if qty > 0.001:
                    pending[op.opration_name][work_order] = qty

            # Carried to everything after it: a piece lost here never arrives there.
            for work_order, balance in by_work_order.items():
                upstream[work_order] = flt(upstream.get(work_order)) + balance["loss"]

        return pending

    def update_operation_pending(self):
        """Write Pending Qty on every In-House operation row.

        All of them, not just the one that changed: loss at one operation moves the
        pending qty of every operation after it."""
        pending = self.pending_by_operation()

        for op in self.operations:
            if op.manufacturing_type != "In-House":
                continue

            value = flt(sum((pending.get(op.opration_name) or {}).values()), 3)
            if flt(op.pending_qty) == value:
                continue

            frappe.db.set_value(
                "Master Work Order Operation", op.name, "pending_qty", value,
                update_modified=False,
            )

    def pending_master_job_card_operations(self):
        """The operations a pending Master Job Card could be raised for.

        Only once every card raised so far has finished and been submitted: a card
        still open is where that work belongs, and a second one beside it would leave
        two cards claiming the same pieces. Submitted as well as complete, because
        until then the Work Order still counts the whole of the first card's quantity
        as outstanding, and validate_job_card_qty() would refuse the second Job Card as
        over-production."""
        if self.docstatus != 1 or self.status in ("Completed", "Closed", "Stopped", "Cancelled"):
            return []

        cards = self.master_job_cards()
        if not cards:
            return []
        if any(card.status != "Completed" or card.docstatus != 1 for card in cards):
            return []

        # An operation left with no card at all -- its only one having been cancelled
        # -- has to be raised again before anything downstream of it carries on. A
        # pending card continues an operation that ran; it does not start one.
        raised = {card.operation_name for card in cards}
        if any(op.opration_name not in raised for op in self.in_house_operations()):
            return []

        by_operation = self.pending_by_operation()

        pending = []
        for op in self.in_house_operations():
            qty = sum((by_operation.get(op.opration_name) or {}).values())
            if qty <= 0.001:
                continue

            pending.append({
                "opration_name": op.opration_name,
                "qty": flt(qty, 3),
            })

        return pending

    def show_pending_master_job_card_button(self):
        """Whether any operation has quantity left to run.

        The same question the Work Order answers in show_create_job_card_button(),
        asked of the Master Job Cards instead: every operation is through, and the
        order still asked for more than they accounted for."""
        return bool(self.pending_master_job_card_operations())

    @frappe.whitelist()
    def make_pending_master_job_cards(self, operations=None):
        """Raise a Master Job Card for each selected operation's outstanding balance.

        Chained in the order the operations run, so the ceiling one operation puts on
        the next applies to this run exactly as it did to the first."""
        operations = frappe.parse_json(operations) if isinstance(operations, str) else (operations or [])

        wanted = {row.get("opration_name") for row in operations if row.get("opration_name")}
        if not wanted:
            frappe.throw("Select at least one operation.", title="Nothing Selected")

        available = {row["opration_name"] for row in self.pending_master_job_card_operations()}

        unavailable = sorted(name for name in wanted if name not in available)
        if unavailable:
            frappe.throw(
                ("Nothing is left to run through: {0}.<br><br>"
                 "Refresh the Master Work Order -- work has been reported against it "
                 "since this form was opened.").format(
                    frappe.bold(", ".join(unavailable))
                ),
                title="Nothing Pending",
            )

        by_operation = self.pending_by_operation()

        created = []
        previous = None
        for op in self.in_house_operations():
            if op.opration_name not in wanted:
                continue

            previous = self.make_pending_master_job_card(
                op.opration_name, by_operation.get(op.opration_name) or {}, previous
            )
            created.append(previous)

        frappe.msgprint(
            ("Raised {0} Master Job Card(s) for the pending qty:<br><br>{1}").format(
                len(created),
                "<br>".join(
                    frappe.utils.get_link_to_form("Master Job Card", name)
                    for name in created
                ),
            ),
            title="Pending Master Job Card",
            indicator="green",
        )

        return created

    def make_pending_master_job_card(self, operation, pending, previous=None):
        master_job_card = frappe.new_doc("Master Job Card")
        master_job_card.master_work_order_number = self.name
        master_job_card.operation_name = operation
        master_job_card.previous_opration_master_job_card = previous
        master_job_card.fetch_from_master_work_order()
        master_job_card.limit_to_pending_qty(pending)
        master_job_card.insert()

        return master_job_card.name

    def final_operation_output(self):
        """What the last In-House operation turned out, per Work Order.

        The ceiling on the Finish: goods that have not come off the end of the line
        cannot be booked as made. Empty when no operation runs in house, and then the
        Work Orders' own quantities are the only ceiling there is."""
        in_house = self.in_house_operations()
        if not in_house:
            return {}

        output = {}
        for row in self.operation_detail_rows(
            in_house[-1].opration_name, ["work_order_number", "completed_qty"]
        ):
            if not row.work_order_number:
                continue
            output[row.work_order_number] = (
                output.get(row.work_order_number, 0.0) + flt(row.completed_qty)
            )

        return output

    def validate_master_job_cards_completed(self):
        """Every In-House operation has to have been raised for.

        Not that every card has finished: part production leaves a pending card in
        draft for the balance, and the 5 that are already through the line should not
        wait on the 5 that are not. How far the Finish may go is
        pending_manufacture_by_work_order()'s job, and it holds it to what the last
        operation actually turned out."""
        in_house = self.in_house_operations()
        if not in_house:
            return

        raised = {card.operation_name for card in self.master_job_cards()}

        missing = [op.opration_name for op in in_house if op.opration_name not in raised]
        if not missing:
            return

        frappe.throw(
            ("No Master Job Card has been raised for: {0}.<br><br>"
             "Create and complete them before finishing.").format(
                frappe.bold(", ".join(name for name in missing if name))
            ),
            title="Operations Not Complete",
        )

    @frappe.whitelist()
    def finish_work_orders(self, rows=None):
        from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry

        self.validate_master_job_cards_completed()

        allowed = self.pending_manufacture_by_work_order()
        if not allowed:
            # Two different situations, and they need different answers: the order is
            # done, or the line has not turned anything out for it yet.
            if self.outstanding_manufacture_by_work_order():
                frappe.throw(
                    self.finish_blocked_reason(), title="Operations Not Complete"
                )

            frappe.throw(
                "There is nothing left to produce.<br><br>"
                "Refresh the Master Work Order -- goods have been produced against it "
                "since this form was opened.",
                title="Nothing Left to Produce",
            )

        rows = frappe.parse_json(rows) if rows else [
            {"work_order_number": work_order, "qty": qty}
            for work_order, qty in allowed.items()
        ]

        # ERPNext raises the finished good for fg_completed_qty less the highest
        # process loss on the Work Order's operations. The quantity entered here is
        # the good pieces wanted, so the loss is added on the way in and ERPNext takes
        # it straight back off -- ask for 5 where 5 were lost and the entry is raised
        # for 10, which books the 5. Entering 5 raw books 5 - 5 = nothing, and the
        # entry is refused for having no finished good at all.
        to_deduct, _booked = self.work_order_process_loss()
        finished = []

        for row in rows:
            work_order = row.get("work_order_number")
            qty = flt(row.get("qty"))
            if not work_order or qty <= 0:
                continue

            if work_order not in allowed:
                frappe.throw(
                    ("{0}: nothing is left to produce against Work Order {1}.<br><br>"
                     "Refresh the Master Work Order -- goods have been produced "
                     "against it since this form was opened.").format(
                        row.get("item_code") or work_order, work_order
                    ),
                    title="Nothing Left to Produce",
                )

            if qty > allowed[work_order]:
                frappe.throw(
                    ("{0}: only {1} is left to produce, but {2} was asked for.<br><br>"
                     "Refresh the Master Work Order -- goods have been produced "
                     "against it since this form was opened.").format(
                        row.get("item_code") or work_order,
                        flt(allowed[work_order], 3),
                        flt(qty, 3),
                    ),
                    title="Qty to Produce Too High",
                )

            status = frappe.db.get_value("Work Order", work_order, "status")
            if status in ("Completed", "Closed", "Stopped", "Cancelled"):
                frappe.throw(
                    ("Work Order {0} is {1} -- nothing can be produced against it.").format(
                        work_order, status
                    )
                )

            stock_entry = frappe.get_doc(
                make_stock_entry(
                    work_order, "Manufacture", qty + flt(to_deduct.get(work_order))
                )
            )
            stock_entry.master_work_order = self.name
            if not stock_entry.get("project"):
                stock_entry.project = self.get("project")
            stock_entry.insert()
            stock_entry.submit()

            finished.append(work_order)

        self.hold_process_loss_to_actual(finished)
        self.update_manufactured_qty()

    def update_manufactured_qty(self):
        """Refresh this order's tables from what the Work Orders now report."""
        produced_total = 0.0

        for row in self.items_to_be_manufacture:
            if not row.work_order_number:
                continue

            work_order = frappe.db.get_value(
                "Work Order",
                row.work_order_number,
                ["produced_qty", "status"],
                as_dict=True,
            ) or frappe._dict()

            produced = flt(work_order.produced_qty)
            produced_total += produced

            # Process Loss Qty is the Master Job Cards' figure and is left alone here.
            # The Finish only accepts a quantity; the Work Order's own loss is what
            # ERPNext booked on the Manufacture entry, which takes the highest loss of
            # any operation rather than their sum.
            row.db_set({
                "manufacture_qty": produced,
                # What was lost is not waiting to be made, so it comes off the pending
                # qty as well as the produced qty. Never below zero: nothing is owed
                # when more has been accounted for than was ever ordered.
                "pending_qty": max(
                    flt(row.qty_to_manufacture) - produced - flt(row.process_loss_qty), 0.0
                ),
                "status": self.item_status(work_order.status),
            }, update_modified=False)

        self.db_set("total_manufacture_qty", produced_total, update_modified=False)

        self.update_production_costs()

        # A Manufacture entry consumes raw material, so refresh that table too.
        self.update_required_item_transfers()
        self.set_status_from_work_orders()

    def update_production_costs(self):
        """Cost of what was produced, off the Manufacture Stock Entries.

        Only Manufacture entries -- a transfer to WIP moves material without
        consuming it, and counting it would charge the same stock twice."""
        entries = frappe.get_all(
            "Stock Entry",
            filters={
                "master_work_order": self.name,
                "purpose": "Manufacture",
                "docstatus": 1,
            },
            fields=["total_outgoing_value", "total_additional_costs"],
        )

        self.db_set({
            "total_raw_material_cost": sum(
                flt(entry.total_outgoing_value) for entry in entries
            ),
            "total_operating_cost": sum(
                flt(entry.total_additional_costs) for entry in entries
            ),
        }, update_modified=False)

    def item_status(self, work_order_status):
        """Map a Work Order status onto the shorter set the item rows carry."""
        if work_order_status in ("Completed", "Stopped"):
            return work_order_status
        if work_order_status in ("Not Started", "Draft", None):
            return "Not Started"
        return "In Process"

    def set_status_from_work_orders(self):
        """Completed only once every Work Order is."""
        work_orders = self.linked_work_orders()
        if not work_orders:
            return

        statuses = frappe.get_all(
            "Work Order", filters={"name": ["in", work_orders]}, pluck="status"
        )
        if not statuses:
            return

        if all(status == "Completed" for status in statuses):
            self.db_set("status", "Completed")
            self.db_set("actual_end_date", frappe.utils.now_datetime())
        elif self.status == "Not Started":
            self.db_set("status", "In Process")

        self.update_production_plan()


    def update_production_plan(self):
        """Push produced qty back to the Production Plan. Our Work Orders carry no
        production_plan link, so ERPNext never does this on its own."""
        if not self.production_plan_number:
            return

        production_plan = frappe.get_doc("Production Plan", self.production_plan_number)
        if production_plan.docstatus != 1 or production_plan.status in ("Closed", "Cancelled"):
            return

        plan_item_by_row = self.map_to_production_plan_items(production_plan)
        if not plan_item_by_row:
            return

        produced = {}
        for row in self.items_to_be_manufacture:
            plan_item = plan_item_by_row.get(row.name)
            if not plan_item:
                continue
            produced[plan_item] = flt(produced.get(plan_item)) + flt(row.manufacture_qty)

        for row in production_plan.po_items:
            if row.name not in produced:
                continue
            row.produced_qty = produced[row.name]
            row.pending_qty = flt(row.planned_qty) - produced[row.name]
            row.db_update()

        # set_status() does not save on its own, so store the status after it.
        production_plan.calculate_total_produced_qty()
        production_plan.set_status()
        production_plan.db_set("status", production_plan.status)

    def map_to_production_plan_items(self, production_plan):
        """Pair each row back to its Production Plan Item. The link is not stored, so
        match on item + BOM + sales order, in row order."""
        available = {}
        for row in production_plan.po_items:
            key = (row.item_code, row.bom_no, row.sales_order or None)
            available.setdefault(key, []).append(row.name)

        mapping = {}
        for row in self.items_to_be_manufacture:
            key = (row.item_code, row.bom_no, row.get("sales_order_number") or None)
            candidates = available.get(key)
            if not candidates:
                continue
            mapping[row.name] = candidates.pop(0)

        return mapping

    # ------------------------------------------------------------------
    # Status -- Close / Stop / Re-open
    # ------------------------------------------------------------------
    @frappe.whitelist()
    def close_work_orders(self):
        from erpnext.manufacturing.doctype.work_order.work_order import close_work_order

        for work_order in self.linked_work_orders():
            status = frappe.db.get_value("Work Order", work_order, "status")
            if status in ("Closed", "Cancelled"):
                continue

            close_work_order(work_order, "Closed")

        self.db_set("status", "Closed")

    @frappe.whitelist()
    def stop_work_orders(self):
        from erpnext.manufacturing.doctype.work_order.work_order import stop_unstop

        for work_order in self.linked_work_orders():
            status = frappe.db.get_value("Work Order", work_order, "status")
            if status in ("Stopped", "Closed", "Cancelled", "Completed"):
                continue

            stop_unstop(work_order, "Stopped")

        self.db_set("status", "Stopped")

    @frappe.whitelist()
    def reopen_work_orders(self):
        from erpnext.manufacturing.doctype.work_order.work_order import stop_unstop

        if self.status == "Closed":
            frappe.throw("A Closed Master Work Order cannot be re-opened.")

        for work_order in self.linked_work_orders():
            status = frappe.db.get_value("Work Order", work_order, "status")
            if status != "Stopped":
                continue

            stop_unstop(work_order, "Resumed")

        self.db_set("status", self.status_after_reopen())

    def linked_work_orders(self):
        return [
            row.work_order_number
            for row in self.items_to_be_manufacture
            if row.work_order_number
        ]

    def status_after_reopen(self):
        work_orders = self.linked_work_orders()
        if not work_orders:
            return "Not Started"

        statuses = frappe.get_all(
            "Work Order",
            filters={"name": ["in", work_orders]},
            pluck="status",
        )
        if statuses and all(status == "Completed" for status in statuses):
            return "Completed"
        if any(status not in ("Not Started", "Draft") for status in statuses):
            return "In Process"

        return "Not Started"

    @frappe.whitelist()
    def make_subcontracted_purchase_order(self):
        from erpnext.stock.get_item_details import get_conversion_factor

        if not self.items_to_be_manufacture:
            frappe.throw(("There are no items to be manufactured to raise a Purchase Order for."))

        transaction_date = frappe.utils.getdate()

        def required_by(planned_start_date):
            date = frappe.utils.getdate(planned_start_date or transaction_date)
            return max(date, transaction_date)

        purchase_order = frappe.new_doc("Purchase Order")
        purchase_order.company = self.company
        purchase_order.transaction_date = transaction_date
        # Recomputed on validate as the earliest item date; set for the draft view.
        purchase_order.schedule_date = required_by(self.planned_start_date)
        purchase_order.master_work_order = self.name
        purchase_order.cost_center = self.cost_center
        purchase_order.project = self.get("project")
        purchase_order.set_warehouse = self.wip_warehouse or self.fg_warehouse
        purchase_order.is_subcontracted = 1

        for row in self.items_to_be_manufacture:
            stock_uom = frappe.db.get_value("Item", row.item_code, "stock_uom")
            uom = row.uom or stock_uom
            conversion_factor = (
                frappe.utils.flt(
                    get_conversion_factor(row.item_code, uom).get("conversion_factor")
                )
                or 1.0
            )

            purchase_order.append("items", {
                # "item_code": row.item_code,
                # "item_name": row.item_name,
                "fg_item" : row.item_code,
                "fg_item_qty": row.qty_to_manufacture,
                "subcontracted_qty" : row.qty_to_manufacture,
                "uom": uom,
                "stock_uom": stock_uom,
                "conversion_factor": conversion_factor,
                # Each row carries the Production Plan Item's own planned start
                # date, the same value ERPNext copies onto its PO rows.
                "schedule_date": required_by(row.planned_start_date or self.planned_start_date),
                "warehouse": row.wip_warehouse or row.fg_warehouse,
                "sales_order": row.get("sales_order_number"),
            })

        return purchase_order

    # --------------------------------
    # Return Components
    # --------------------------------

    @frappe.whitelist()
    def get_return_items(self):
        result = []
        for row in self.items_to_be_manufacture:
            work_order = row.work_order_number
            wo_doc = frappe.get_cached_doc("Work Order", work_order)

            items = []
            for d in wo_doc.required_items:
                max_returnable = flt(d.transferred_qty) - flt(d.consumed_qty) - flt(d.returned_qty)
                if max_returnable <= 0:
                    continue
                items.append({
                    "item_code": d.item_code,
                    "item_name": d.item_name,
                    "transferred_qty": d.transferred_qty,
                    "consumed_qty": d.consumed_qty,
                    "returned_qty": d.returned_qty,
                    "max_returnable": max_returnable,
                    "qty": max_returnable,
                    "work_order_number": work_order,
                })

            if items:
                result.append({
                    "work_order": work_order,
                    "bom_no": wo_doc.bom_no,
                    "items": items,
                })

        return result

    @frappe.whitelist()
    def create_return_stock_entry(self, items):
        import json
        from erpnext.stock.doctype.stock_entry.stock_entry import get_available_materials

        if isinstance(items, str):
            items = json.loads(items)

        # Group selected rows by work order, since one Stock Entry is created per WO.
        rows_by_wo = {}
        for r in items:
            qty = flt(r.get("qty"))
            if qty <= 0:
                continue
            rows_by_wo.setdefault(r.get("work_order_number"), []).append(r)

        if not rows_by_wo:
            frappe.throw("Enter a Qty to Return for at least one item.")

        created_entries = []

        for row in self.items_to_be_manufacture:
            work_order = row.work_order_number
            selected_rows = rows_by_wo.get(work_order)
            if not selected_rows:
                continue

            non_consumed_items = get_available_materials(work_order)
            if not non_consumed_items:
                continue

            qty_by_item = {r["item_code"]: flt(r["qty"]) for r in selected_rows}

            wo_doc = frappe.get_cached_doc("Work Order", work_order)

            stock_entry = frappe.new_doc("Stock Entry")
            stock_entry.from_bom = 1
            stock_entry.is_return = 1
            stock_entry.work_order = work_order
            stock_entry.purpose = "Material Transfer for Manufacture"
            stock_entry.bom_no = wo_doc.bom_no
            stock_entry.add_transfered_raw_materials_in_items()
            stock_entry.set_stock_entry_type()

            # Keep only user-selected items, cap qty at what's actually available.
            filtered_items = []
            for item in stock_entry.items:
                selected_qty = qty_by_item.get(item.item_code)
                if not selected_qty:
                    continue
                available = flt(item.qty)
                item.qty = min(selected_qty, available)
                item.transfer_qty = item.qty * flt(item.conversion_factor or 1)
                filtered_items.append(item)

            if not filtered_items:
                continue

            stock_entry.items = filtered_items
            stock_entry.master_work_order = self.name
            stock_entry.insert()
            stock_entry.submit()
            created_entries.append(stock_entry.name)

        if not created_entries:
            frappe.throw("No Stock Return Entry could be created for the selected quantities.")

        return created_entries


@frappe.whitelist()
def make_master_work_order(production_plan_id):
    doc = frappe.new_doc("Master Work Order")
    doc.production_plan_number = production_plan_id
    return doc._make_master_work_order()


def set_default_warehouses(doc):
    from erpnext.manufacturing.doctype.work_order.work_order import get_default_warehouse
    default_warehouses = get_default_warehouse(doc.company)

    for field in ["wip_warehouse", "fg_warehouse", "scrap_warehouse"]:
        if not doc.get(field) and default_warehouses.get(field):
            doc.set(field, default_warehouses.get(field))