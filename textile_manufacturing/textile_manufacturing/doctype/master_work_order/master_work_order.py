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
        self.validate_manufacturing_type_change()

        for row in self.items_to_be_manufacture:
            if not row.qty_to_manufacture:
                frappe.throw(
                    ("Row {0}: Qty to Manufacture is not set for item {1}").format(
                        row.idx, row.item_code
                    )
                )


    def on_submit(self):
        self.create_work_orders()
        self.set_operation_qty_from_work_orders()
        self.create_master_job_cards()

        status = "Not Started" if not self.skip_material_transfer_to_wip_warehouse else "In Process"
        self.db_set("status", status)

    def before_update_after_submit(self):
        # validate() does not run on a submitted doc, and Manufacturing Type is an
        # allow-on-submit field -- so the check has to be hung here as well, which is
        # where the change it guards against is actually made.
        self.validate_manufacturing_type_change()

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

    def validate_manufacturing_type_change(self):
        """An In-House operation that already has a Master Job Card stays In-House.

        Turning it Out House, or clearing it, would leave the card standing with
        nothing on the order left to explain it -- and the Work Order Operation row
        it books its work against is still there, so the card goes on running while
        the order says the operation is the supplier's. The card is cancelled or
        deleted first, and the type moves after that."""
        before = self.get_doc_before_save()
        if not before:
            return

        # Read off the saved doc, by row, so that a row whose operation was renamed in
        # the same save is still matched against the card raised for the old name.
        was_in_house = {
            row.name: row.opration_name
            for row in before.operations
            if row.manufacturing_type == "In-House" and row.opration_name
        }
        if not was_in_house:
            return

        for row in self.operations:
            if row.manufacturing_type == "In-House":
                continue

            operation = was_in_house.get(row.name)
            if not operation:
                continue

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
                continue

            frappe.throw(
                ("Row {0}: Operation {1} cannot be moved off In-House -- "
                 "Master Job Card {2} is raised against it.<br><br>"
                 "Cancel or delete the card first, then change the Manufacturing Type.").format(
                    row.idx,
                    frappe.bold(operation),
                    ", ".join(
                        frappe.utils.get_link_to_form("Master Job Card", card)
                        for card in cards
                    ),
                ),
                title="Master Job Card Exists",
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
                # Set from the off, not left blank until the first Finish: the row
                # carries a status column and an empty one reads as a bug on a form
                # whose Work Order is already running.
                "status": self.item_status(work_order.status),
            }, update_modified=False)


    def set_in_house_operations(self, work_order):
        if not work_order.bom_no:
            return

        work_order.set_work_order_operations()

        in_house = [
            op for op in self.operations
            if op.manufacturing_type == "In-House" and op.opration_name
        ]
        wanted = {op.opration_name for op in in_house}

        work_order.operations = [
            op for op in work_order.operations if op.operation in wanted
        ]

        self.append_operations_missing_from_work_order(work_order, in_house)

    def append_operations_missing_from_work_order(self, work_order, operations):
        """Put the operations no BOM supplied onto the Work Order.

        set_work_order_operations() builds the table out of the BOM, so an operation
        the planner put on the order by hand is in none of it -- and the Work Order
        Operation row is the only thing a Job Card can be raised against. It runs on
        every Work Order of the order whatever any one BOM happens to carry, which is
        what the planner asked for by adding it to an order-wide table.

        Sequence Id is carried on from the last row rather than taken off the
        operation: ERPNext's validate_operations_sequence() will only have them run
        1, 2, 3 down the table or be blank throughout, and an operation added by hand
        has whatever number -- usually none -- the planner left on it."""
        on_work_order = {op.operation for op in work_order.operations}
        last = work_order.operations[-1] if work_order.operations else None
        sequence_id = cint(last.sequence_id) if last else 0

        for op in operations:
            if op.opration_name in on_work_order:
                continue

            values = self.work_order_operation_values(op)
            # Blank throughout stays blank throughout -- ERPNext then numbers the
            # whole table off idx for itself.
            values["sequence_id"] = sequence_id + 1 if sequence_id else 0
            sequence_id = values["sequence_id"]

            work_order.append("operations", values)
            on_work_order.add(op.opration_name)

    def work_order_operation_values(self, operation):
        """The Work Order Operation row one of this order's operations becomes."""
        return {
            "operation": operation.opration_name,
            "workstation": operation.workstation,
            "workstation_type": operation.workstation_type,
            "sequence_id": cint(operation.opration_sequence_no),
            "time_in_mins": flt(operation.standerd_time),
            "hour_rate": flt(operation.hour_rate),
            "status": "Pending",
            "completed_qty": 0,
            "process_loss_qty": 0,
        }

    def work_orders_running_operation(self, operation):
        """The Work Orders of this order that carry the operation.

        Which is what decides whether an item runs it, rather than the BOM: the Work
        Order Operation row is what a Job Card is booked against, so an item runs an
        operation exactly when its Work Order carries it. An operation added to the
        order by hand is in no BOM at all and reaches every Work Order."""
        work_orders = self.linked_work_orders()
        if not work_orders:
            return set()

        return set(frappe.get_all(
            "Work Order Operation",
            filters={
                "parent": ["in", work_orders],
                "parenttype": "Work Order",
                "operation": operation,
            },
            pluck="parent",
        ))

    def set_operation_qty_from_work_orders(self):
        """Fill in the qty of any In-House operation the BOMs did not account for."""
        for op in self.operations:
            if op.manufacturing_type != "In-House":
                continue

            self.set_operation_qty_to_manufacture(op)

    def set_operation_qty_to_manufacture(self, operation):
        """What the order asks of an operation the BOMs never supplied.

        set_operations() works this out per BOM when the order is built, and an
        operation added by hand was in none of them -- so the row would sit at
        nothing, and pending_master_job_card_operations(), which reads exactly this
        field, would never offer it. It runs on every Work Order it reached, so it is
        asked for the whole of what those were raised for.

        A figure already on the row is left alone: it is either the BOMs' or the
        planner's, and both outrank this."""
        if flt(operation.total_qty_to_manufacture):
            return

        running = self.work_orders_running_operation(operation.opration_name)
        if not running:
            return

        qty = sum(
            flt(row.qty_to_manufacture)
            for row in self.items_to_be_manufacture
            if row.work_order_number in running
        )
        if qty <= 0:
            return

        operation.total_qty_to_manufacture = qty
        operation.db_set(
            "total_qty_to_manufacture", qty, update_modified=False
        )


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

    @frappe.whitelist()
    def set_available_qty(self):
        for row in self.required_items:
            row.available_qty = self.get_source_warehouse_stock(row.item_code)

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

            # After the Work Orders have it and before the card is raised: the qty is
            # read off the Work Orders that took the operation, and the card is
            # raised for what the row then says.
            self.set_operation_qty_to_manufacture(op)

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
        """Add the operation to a submitted Work Order, and raise its Job Card.

        Whatever the BOM carries. The Work Order Operation row is what a Job Card is
        booked against, so an operation the planner adds to the order has to reach
        every Work Order on it -- and the BOM has nothing to say about that, because
        the operation was added to the order rather than to the BOM.

        Skipping the ones no BOM carried, which is what this used to do, left the
        planner with an operation that looked added and was not: a Master Job Card was
        still raised for it, and it failed at the far end on submit with "the
        operation is not on the Work Order, so there is no operation row to book the
        work against"."""
        from erpnext.manufacturing.doctype.work_order.work_order import create_job_card

        work_order = frappe.get_doc("Work Order", work_order)
        if work_order.docstatus != 1:
            return
        if any(op.operation == operation.opration_name for op in work_order.operations):
            return

        row = work_order.append(
            "operations", self.work_order_operation_values(operation)
        )
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

    def hold_process_loss_to_actual(self, work_orders=None):
        """Hold each Work Order's process loss to what was really lost, and re-read
        its status from it.

        ERPNext keeps the figure itself, in set_process_loss_qty(), by totalling
        the loss over the submitted Manufacture entries -- and that is wrong here
        three ways over. Over: every entry carries the same figure, so part
        production counts the same loss once per entry. Under: the figure each
        entry carries is the highest loss on any single operation row, and
        different pieces die at different operations -- 5 rejected at one and 1
        more at the next is 6 pieces gone, not 5, which left the Work Order a piece
        short of Completed for good. Late: until the first Finish there are no
        entries at all, so an order destroyed outright is never finished, never
        corrected, and never leaves In Process.

        The truth is the item row's own Process Loss Qty, the sum over every
        completed card, so the Work Order is held to that whenever it moves and its
        status re-read -- Completed once made plus lost accounts for the order.
        work_orders narrows it to the ones a Finish has just touched; without it
        every Work Order on the order is brought back into line."""
        for row in self.items_to_be_manufacture:
            if not row.work_order_number:
                continue
            if work_orders is not None and row.work_order_number not in set(work_orders):
                continue

            actual = flt(row.process_loss_qty)
            booked = flt(frappe.db.get_value(
                "Work Order", row.work_order_number, "process_loss_qty"
            ))
            if abs(booked - actual) <= 0.001:
                continue

            doc = frappe.get_doc("Work Order", row.work_order_number)
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

        # No operation is routed anywhere: no Master Job Card is raised
        if not any(row.manufacturing_type for row in self.operations):
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
                # The item row's own figure -- every operation's process loss and
                # rejects added up, which is what the order really lost. Not
                # to_deduct: that is ERPNext's, the highest loss on any single
                # operation, and it reads 5 where 5 were rejected at one operation
                # and 1 more at the next. It is still what the Manufacture entry is
                # raised against below, because ERPNext takes exactly that much back
                # off -- but it is the wrong number to show anybody.
                "process_loss_qty": flt(row.process_loss_qty),
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

    def operation_figures(self):
        """What each operation row should read, per Work Order.

        Completed, Process Loss and Pending, and they always account for the row's
        Total Qty to Manufacture -- completed + loss + pending = total, on every
        row, always. A row that does not add up is a row nobody can check.

            Completed    = every card of this operation, added up
            Process Loss = what never came out of this operation
            Pending      = Qty to Manufacture - Completed - Process Loss

        Qty to Manufacture never moves -- it is what the order asked of this
        operation, and it stays that whatever happens on the floor. The three
        always account for it, so every row adds up.

        Completed and Pending are plain. Process Loss is the one that needs
        saying: it is not only what this operation destroyed, but everything that
        stopped it running the whole order. Its own rejects are gone, and so is
        material destroyed at an operation the cloth passes before reaching here --
        that never arrives and never will. 5 destroyed ahead of it and 1 rejected
        here leaves the row 4 completed, 6 lost, nothing pending: 4 and 6 make the
        10 it was asked for.

        Which operations the cloth passes first is settled off the reported
        quantities, not off any routing or sequence -- none is declared and the
        floor keeps to none. What each operation receives it passes on, so an
        operation that has handled more of the order is one the cloth reaches
        earlier, and handled is Completed and destroyed together. An operation that
        has handled the same or less is behind or alongside this one, and what it
        destroyed had already come through here -- so it is not charged here, and
        5 run of 10 with 1 rejected further down still leaves 5 to run, not 4.

        Added up over every card the operation was run on, the card the order was
        raised with and each pending card after it, so 4 made on one and 4 on the
        next reads as 8 and not 4."""
        ordered = {
            row.work_order_number: flt(row.qty_to_manufacture)
            for row in self.items_to_be_manufacture
            if row.work_order_number
        }
        balances = self.operation_balances()

        figures = {}
        for op in self.in_house_operations():
            name = op.opration_name
            figures[name] = {}

            for work_order, balance in (balances.get(name) or {}).items():
                handled = balance["completed"] + balance["loss"]

                # Destroyed where the cloth passes before it gets here.
                never_arrived = 0.0
                for other_name, by_work_order in balances.items():
                    if other_name == name:
                        continue
                    other = by_work_order.get(work_order)
                    if not other:
                        continue
                    if other["completed"] + other["loss"] > handled + 0.001:
                        never_arrived += other["loss"]

                loss = balance["loss"] + never_arrived
                figures[name][work_order] = {
                    "completed": balance["completed"],
                    "loss": loss,
                    "pending": max(
                        flt(ordered.get(work_order)) - balance["completed"] - loss,
                        0.0,
                    ),
                }

        return figures

    def pending_by_operation(self):
        """What each operation still has to run, per Work Order.

        The Pending column of operation_figures(), which is where the arithmetic
        and its reasoning live. Kept apart because this is the figure the pending
        Master Job Card dialog offers, and it is only ever the part of the row that
        is still work."""
        return {
            name: {
                work_order: figure["pending"]
                for work_order, figure in by_work_order.items()
                if figure["pending"] > 0.001
            }
            for name, by_work_order in self.operation_figures().items()
        }

    def refresh_item_status(self):
        """Bring every item row's status back in line with its Work Order.

        Called whenever a Master Job Card reports, not only at the Finish: an
        operation running is exactly when the row moves off Not Started, and
        leaving it until goods are booked strands the column for the whole run."""
        for row in self.items_to_be_manufacture:
            if not row.work_order_number:
                continue

            status = self.item_status(
                frappe.db.get_value("Work Order", row.work_order_number, "status")
            )
            if row.status == status:
                continue

            frappe.db.set_value(
                "Master Work Order Item", row.name, "status", status,
                update_modified=False,
            )

    def update_operation_rows(self):
        """Write Completed, Process Loss and Pending on every In-House operation row.

        The three figures a card reporting can move, written together off
        operation_figures() so they always agree:

            Completed    = every card of that operation added up
            Process Loss = every card of that operation added up
            Pending      = Qty to Manufacture - Completed - Process Loss

        Qty to Manufacture is not touched. It is what the order asked of the
        operation and it stays that.

        Every row, not only the one whose card reported, so a cancelled card
        elsewhere cannot leave a stale figure behind."""
        figures = self.operation_figures()

        for op in self.operations:
            if op.manufacturing_type != "In-House":
                continue

            by_work_order = figures.get(op.opration_name) or {}
            if by_work_order:
                values = {
                    "completed_qty": flt(
                        sum(f["completed"] for f in by_work_order.values()), 3
                    ),
                    "process_loss_qty": flt(
                        sum(f["loss"] for f in by_work_order.values()), 3
                    ),
                    "pending_qty": flt(
                        sum(f["pending"] for f in by_work_order.values()), 3
                    ),
                }
            else:
                # No live card for this operation: the last one was cancelled, or
                # none was ever raised. Nothing made, nothing lost, and the whole of
                # what the order asked of it owed again.
                #
                # Written out rather than summed off an empty reckoning, which reads
                # Pending as zero and leaves the row not adding up -- 0 completed, 0
                # lost and 0 pending against a Total Qty to Manufacture of 10, an
                # operation that has run nothing and owes nothing.
                values = {
                    "completed_qty": 0.0,
                    "process_loss_qty": 0.0,
                    "pending_qty": flt(op.total_qty_to_manufacture, 3),
                }

            if all(flt(op.get(field)) == value for field, value in values.items()):
                continue

            frappe.db.set_value(
                "Master Work Order Operation", op.name, values,
                update_modified=False,
            )

    def pending_master_job_card_operations(self):
        """The operations a pending Master Job Card could be raised for.

        What no card has claimed yet. Per operation:

            Qty to Manufacture on the Master Work Order operation row
              - the Qty to Manufacture of every Master Job Card raised for it

        Nothing comes off for process loss or rejects. A card's Qty to Manufacture is
        already what it accounted for -- completed + process loss + rejected -- so
        those are inside it, and taking them off again would offer the same pieces to
        be made twice.

        A card being open does not stand in the way. It claims its own quantity and
        nothing more, so the balance beside it belongs to nobody and can be raised
        while the first is still being worked -- an order for 10 whose card is typed
        down to 5 offers the other 5 at once. Two cards never claim the same pieces,
        because what one takes is subtracted before the next is offered, and the sum
        of them stays inside the order, which is the same sum ERPNext holds Job Cards
        to in validate_job_card_qty()."""
        if self.docstatus != 1 or self.status in ("Completed", "Closed", "Stopped", "Cancelled"):
            return []

        cards = self.master_job_cards()
        if not cards:
            return []

        # An operation left with no card at all -- its only one having been cancelled
        # -- has to be raised again before anything downstream of it carries on. A
        # pending card continues an operation that ran; it does not start one.
        raised = {card.operation_name for card in cards}
        if any(op.opration_name not in raised for op in self.in_house_operations()):
            return []

        # Nothing may be raised once there is no cloth left to run. What has come
        # off the end of the line, plus what every operation destroyed, accounts
        # for the whole order: the rest is gone, and a card raised for it could
        # never be finished. The rows should read nothing pending by then anyway --
        # this is the order-level check behind the per-operation one.
        if not self.outstanding_after_loss():
            return []

        # What each operation still owes, straight off its own row. Every card writes
        # its operation's Completed, Process Loss and Pending back there as it
        # finishes -- see update_operation_rows() -- so the row is the figure, and the
        # dialog offers exactly what the form shows.
        #
        # Read from the database rather than off self.operations: a card reporting
        # writes the row behind whatever document is in hand, so an instance loaded
        # before that would offer figures the form has already moved past.
        ordered = {
            row.opration_name: flt(row.total_qty_to_manufacture)
            for row in frappe.get_all(
                "Master Work Order Operation",
                filters={"parent": self.name, "parenttype": "Master Work Order"},
                fields=["opration_name", "total_qty_to_manufacture"],
            )
        }
        claimed = self.qty_claimed_by_operation()

        pending = []
        for op in self.in_house_operations():
            qty = flt(ordered.get(op.opration_name)) - flt(claimed.get(op.opration_name))
            if qty <= 0.001:
                continue

            pending.append({
                "opration_name": op.opration_name,
                "qty": flt(qty, 3),
            })

        return pending

    def qty_claimed_by_operation(self):
        """The Qty to Manufacture of every card of an operation, added up.

        The card's own figure, not the order's: a run of 5 off an order for 10 is
        saved on the card as 5, so what it claims is the 5 it ran and not the 10 it
        was raised for.

        Nothing is taken off for process loss or rejects. A piece can only be
        destroyed after it has been taken, so the loss is already inside the qty the
        card claims -- subtracting it again would hand the same pieces back to be
        made a second time.

        A card being worked claims its qty just as a finished one does, which is what
        lets a second be raised beside it for the balance. A cancelled card claims
        nothing."""
        claimed = {}
        for card in frappe.get_all(
            "Master Job Card",
            filters={"master_work_order_number": self.name, "docstatus": ["<", 2]},
            fields=["operation_name", "total_qty_to_manufacture"],
        ):
            claimed[card.operation_name] = (
                flt(claimed.get(card.operation_name)) + flt(card.total_qty_to_manufacture)
            )

        return claimed

    def outstanding_after_loss(self):
        """Work Orders with cloth still to run, per Work Order.

        An order of 10 that has turned 4 off the end of the line and destroyed 6
        has nothing left: 4 and 6 account for it. One that has turned 4 out and
        destroyed 1 has 5 still to run. Measured off what has cleared every
        operation -- final_operation_output() -- rather than off any one of them,
        because a piece is only through when all of them have had it.

        The item rows are read from the database rather than off this document: a
        card reporting writes Process Loss Qty behind whatever instance is in hand,
        and an older one would still think the cloth was there."""
        output = self.final_operation_output()

        outstanding = {}
        for row in frappe.get_all(
            "Master Work Order Item",
            filters={"parent": self.name, "parenttype": "Master Work Order"},
            fields=["work_order_number", "qty_to_manufacture", "process_loss_qty"],
        ):
            if not row.work_order_number:
                continue

            qty = (
                flt(row.qty_to_manufacture)
                - flt(output.get(row.work_order_number))
                - flt(row.process_loss_qty)
            )
            if qty > 0.001:
                outstanding[row.work_order_number] = qty

        return outstanding

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
        """What the line has turned all the way out, per Work Order.

        The ceiling on the Finish: a piece is only made once it has cleared every
        In-House operation that runs it, so the ceiling is the least any of them
        has completed -- which needs no notion of which operation runs last. Held
        to the operations that actually run the Work Order: the cards carry a row
        per Work Order they run, and an item is not held to an operation it never
        visits. Empty when no operation runs in house, and then the Work Orders'
        own quantities are the only ceiling there is."""
        in_house = self.in_house_operations()
        if not in_house:
            return {}

        balances = self.operation_balances()

        output = {}
        for op in in_house:
            for work_order, balance in (balances.get(op.opration_name) or {}).items():
                completed = flt(balance["completed"])
                if work_order in output:
                    output[work_order] = min(output[work_order], completed)
                else:
                    output[work_order] = completed

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
        """Completed once every Work Order is, and the Out House work has come back."""
        work_orders = self.linked_work_orders()
        if not work_orders:
            return

        statuses = frappe.get_all(
            "Work Order", filters={"name": ["in", work_orders]}, pluck="status"
        )
        if not statuses:
            return

        finished = all(status == "Completed" for status in statuses)

        if finished and not self.outstanding_out_house_qty():
            self.db_set("status", "Completed")
            self.db_set("actual_end_date", frappe.utils.now_datetime())
        elif self.status in ("Not Started", "Completed"):
            # Off Completed as readily as on to it. ERPNext works its Work Order's
            # status out from scratch every time something moves -- get_status() --
            # so cancelling a Manufacture entry takes the order back off Completed
            # of its own accord, and the wrapper has to follow or it would sit
            # Completed over Work Orders that are not. It is what lets the Finish be
            # cancelled and the run put right afterwards.
            #
            # Closed, Stopped and Cancelled are set by hand and are nobody's to undo
            # here, which is why they are named rather than everything-but-Completed.
            self.db_set("status", "In Process")
            self.db_set("actual_end_date", None)

        self.update_production_plan()

    def out_house_operations(self):
        return [
            row for row in self.operations if row.manufacturing_type == "Out House"
        ]

    def outstanding_out_house_qty(self):
        if not self.out_house_operations():
            return

        receipts = frappe.get_all(
            "Subcontracting Receipt",
            filters={"master_work_order": self.name, "docstatus": 1},
            pluck="name",
        )

        received = {}
        if receipts:
            for row in frappe.get_all(
                "Subcontracting Receipt Item",
                filters={"parent": ["in", receipts]},
                fields=["item_code", "qty"],
            ):
                received[row.item_code] = flt(received.get(row.item_code)) + flt(row.qty)

        outstanding = {}
        for row in self.items_to_be_manufacture:
            # What the supplier owes is what the order asked for less what was
            # destroyed: a piece that no longer exists is never coming back, and
            # holding the order open for it would leave it open for good.
            owed = flt(row.qty_to_manufacture) - flt(row.process_loss_qty)
            short = owed - flt(received.get(row.item_code))
            if short > 0.001:
                outstanding[row.item_code] = short

        return outstanding


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
                "fg_item": row.item_code,
                # What actually goes out to him is what the line has turned out. An
                # order for 10 that made 8 sends 8 -- the other 2 were destroyed and
                # there is nothing there to send, and a Purchase Order raised for
                # them could never be received against. Where nothing has been
                # produced yet the order's own qty stands in, so the Purchase Order
                # can be raised ahead of the run rather than only after it.
                "fg_item_qty": flt(row.manufacture_qty) or flt(row.qty_to_manufacture),
                # The service line's own qty. One unit of the operation is bought per
                # unit made, so it matches the finished goods qty -- ERPNext divides the
                # two for the row's conversion factor, and any other figure scales the
                # Subcontracting Order's quantity by the difference.
                "qty": flt(row.manufacture_qty) or flt(row.qty_to_manufacture),
                # subcontracted_qty is deliberately not set. It is ERPNext's running
                # count of how much of the row a Subcontracting Order has already taken,
                # kept by update_subcontracted_quantity_in_po() as each one is
                # submitted, and it has to start at nothing. Filled in here it made
                # every row read as already subcontracted, and Create > Subcontracting
                # Order dropped them without a word -- its condition is
                # qty != subcontracted_qty, and a fully matching order was refused
                # outright as "already fully subcontracted".
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