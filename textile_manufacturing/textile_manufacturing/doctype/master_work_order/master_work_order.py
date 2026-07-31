# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class MasterWorkOrder(Document):
    def validate(self):
        for row in self.items_to_be_manufacture:
            if not row.qty_to_manufacture:
                frappe.throw(
                    ("Row {0}: Qty to Manufacture is not set for item {1}").format(
                        row.idx, row.item_code
                    )
                )

        # for row in self.operations:
        #     if not row.manufacturing_type:
        #         frappe.throw(
        #             ("Row {0}: Manufacturing Type is not set for operation {1}").format(
        #                 row.idx, row.opration_name
        #             )
        #         )


    def on_submit(self):
        self.create_work_orders()

    def create_work_orders(self):
        for row in self.items_to_be_manufacture:
            work_order = frappe.new_doc("Work Order")
            work_order.company = self.company
            work_order.production_item = row.item_code
            work_order.bom_no = row.bom_no
            work_order.qty = row.qty_to_manufacture
            work_order.source_warehouse = row.source_warehouse
            work_order.wip_warehouse = row.wip_warehouse
            work_order.fg_warehouse = row.fg_warehouse
            work_order.scrap_warehouse = row.scrap_warehouse
            work_order.use_multi_level_bom = self.use_multi_level_bom
            work_order.planned_start_date = self.posting_date or frappe.utils.now_datetime()

            self.set_in_house_operations(work_order)

            work_order.insert()

            row.db_set("work_order_number", work_order.name, update_modified=False)


    def set_in_house_operations(self, work_order):
        """Fetch operations from the BOM but pass only the In-House ones to the
        Work Order. Out House (subcontracted) operations are skipped."""
        if not work_order.bom_no:
            return

        work_order.set_work_order_operations()

        in_house_operations = [
            op for op in work_order.operations if not op.is_subcontracted
        ]

        # Re-number so the sequence has no gaps left by the skipped operations.
        for sequence_id, op in enumerate(in_house_operations, start=1):
            op.idx = sequence_id
            op.sequence_id = sequence_id

        work_order.operations = in_house_operations


    def on_cancel(self):
        for row in self.items_to_be_manufacture:
            if not row.work_order_number:
                continue

            wo = frappe.get_doc("Work Order", row.work_order_number)
            if wo.docstatus == 1:
                wo.cancel()


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

        # Combine the same operation coming from multiple BOMs into a single row,
        # summing its standard time and the qty to manufacture.
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

        # Group by item so the same raw material coming from multiple BOMs is
        # shown as a single row with the total required qty.
        consolidated = {}
        for item in items:
            row = consolidated.setdefault(item.item_code, {
                "item_code": item.item_code,
                "item_name": item.item_name,
                "uom": item.uom,
                "requried_qty": 0,
            })
            row["requried_qty"] += item.qty or 0

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

        # Group by item so scrap coming from multiple BOMs is shown as a single row.
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


    @frappe.whitelist()
    def start_job_card(self):
        self.db_set("actual_start_date", frappe.utils.now_datetime())
        self.create_master_job_cards()

    def create_master_job_cards(self):
        """Create one Master Job Card per In-House operation. Out House
        (subcontracted) operations never get a Master Job Card.

        The operations table is already combined (one row per operation), so a
        simple filter on manufacturing type is all that is needed here."""
        created = []

        # Operations are ordered by sequence, so the last In-House card we know
        # about is the "previous operation" reference for the next one.
        previous_master_job_card = None

        for op in self.operations:
            if op.manufacturing_type != "In-House":
                continue
            # Skip operations whose Master Job Card was already created earlier,
            # but still carry it forward as the previous-operation reference.
            if op.master_job_card_number:
                previous_master_job_card = op.master_job_card_number
                continue

            master_job_card = frappe.new_doc("Master Job Card")
            master_job_card.master_work_order_number = self.name
            master_job_card.operation_name = op.opration_name
            master_job_card.previous_opration_master_job_card = previous_master_job_card
            master_job_card.fetch_from_master_work_order()
            master_job_card.insert()

            op.db_set("master_job_card_number", master_job_card.name, update_modified=False)
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