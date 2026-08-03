# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import flt


class MasterWorkOrder(Document):
    def validate(self):
        self.validate_unique_production_plan()

        for row in self.items_to_be_manufacture:
            if not row.qty_to_manufacture:
                frappe.throw(
                    ("Row {0}: Qty to Manufacture is not set for item {1}").format(
                        row.idx, row.item_code
                    )
                )

        for row in self.operations:
            if not row.manufacturing_type:
                frappe.throw(
                    ("Row {0}: Manufacturing Type is not set for operation {1}").format(
                        row.idx, row.opration_name
                    )
                )


    def on_submit(self):
        # Work Orders first: submitting them is what raises the Job Cards the Master
        # Job Cards then take up.
        self.create_work_orders()
        self.create_master_job_cards()

        if not self.skip_material_transfer_to_wip_warehouse:
            self.db_set("status", "Not Started")
        else:
            self.db_set("status", "In Process")

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
            work_order.submit()

            row.db_set("work_order_number", work_order.name, update_modified=False)


    def set_in_house_operations(self, work_order):
        """Pass only the In-House BOM operations to the Work Order."""
        if not work_order.bom_no:
            return

        work_order.set_work_order_operations()

        in_house_operations = [
            op for op in work_order.operations if not op.is_subcontracted
        ]

        work_order.operations = in_house_operations


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
                "is_subcontracted",
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
                "manufacturing_type": "Out House" if op.is_subcontracted else "In-House",
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
    def start_material_transfer(self, rows=None):
        self.transfer_material_for_work_orders(rows)
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

    def transfer_material_for_work_orders(self, rows=None):
        from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry

        if not self.can_transfer_material():
            return

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

            stock_entry = frappe.get_doc(
                make_stock_entry(work_order, "Material Transfer for Manufacture", qty)
            )
            stock_entry.master_work_order = self.name
            if not stock_entry.get("project"):
                stock_entry.project = self.get("project")
            stock_entry.insert()
            stock_entry.submit()

        self.update_transferred_qty()

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
        # Transferring material puts the Work Orders In Process, so this order has to
        # move with them -- it was sitting at Not Started until the Finish.
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
    def pending_manufacture_by_work_order(self):
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

            qty = ceiling - flt(row.manufacture_qty)
            if qty <= 0:
                continue

            pending[row.work_order_number] = qty

        return pending

    def master_job_cards(self):
        """The Master Job Cards raised against this order, newest last."""
        return frappe.get_all(
            "Master Job Card",
            filters={"master_work_order_number": self.name, "docstatus": ["<", 2]},
            fields=["name", "operation_name", "status"],
            order_by="creation",
        )

    @frappe.whitelist()
    def pending_master_job_card_operations(self):
        """Operations whose balance can be carried onto a fresh Master Job Card.

        Read off the cards themselves, not the operation rows: a pending card carries
        the same operation name, so sync_to_master_work_order() puts that row back to
        Pending as soon as one is raised."""
        if flt(self.total_manufacture_qty) == flt(self.total_qty_to_manufacture):
            return []

        cards = self.master_job_cards()
        if not cards or any(card.status != "Completed" for card in cards):
            return []

        return [
            {
                "opration_name": op.opration_name,
                "pending_qty": flt(op.pending_qty),
            }
            for op in self.operations
            if op.manufacturing_type == "In-House" and flt(op.pending_qty) > 0
        ]

    def validate_master_job_cards_completed(self):
        in_house = [op for op in self.operations if op.manufacturing_type == "In-House"]
        if not in_house:
            return

        cards = self.master_job_cards()

        missing = [
            op.opration_name
            for op in in_house
            if op.opration_name not in {card.operation_name for card in cards}
        ]
        if missing:
            frappe.throw(
                ("No Master Job Card has been raised for: {0}.<br><br>"
                 "Create and complete them before finishing.").format(
                    frappe.bold(", ".join(name for name in missing if name))
                ),
                title="Operations Not Complete",
            )

        incomplete = [card for card in cards if card.status != "Completed"]
        if not incomplete:
            return

        lines = "<br>".join(
            "{0} -- {1} is {2}".format(
                frappe.utils.get_link_to_form("Master Job Card", row.name),
                frappe.bold(row.operation_name or ""),
                row.status,
            )
            for row in incomplete
        )
        frappe.throw(
            ("Operations are not complete for this Master Work Order.<br><br>{0}"
             "<br><br>Complete them before finishing.").format(lines),
            title="Operations Not Complete",
        )

    @frappe.whitelist()
    def finish_work_orders(self, rows=None):
        from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry

        self.validate_master_job_cards_completed()

        allowed = self.pending_manufacture_by_work_order()
        if not allowed:
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

            stock_entry = frappe.get_doc(make_stock_entry(work_order, "Manufacture", qty))
            stock_entry.master_work_order = self.name
            if not stock_entry.get("project"):
                stock_entry.project = self.get("project")
            stock_entry.insert()
            stock_entry.submit()

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
                ["produced_qty", "process_loss_qty", "status"],
                as_dict=True,
            ) or frappe._dict()

            produced = flt(work_order.produced_qty)
            produced_total += produced

            row.db_set({
                "manufacture_qty": produced,
                "process_loss_qty": flt(work_order.process_loss_qty),
                "pending_qty": flt(row.qty_to_manufacture) - produced,
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
                "item_code": row.item_code,
                "item_name": row.item_name,
                "qty": row.qty_to_manufacture,
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