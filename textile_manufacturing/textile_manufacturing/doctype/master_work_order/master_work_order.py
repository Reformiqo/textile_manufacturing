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

        for row in self.operations:
            if not row.manufacturing_type:
                frappe.throw(
                    ("Row {0}: Manufacturing Type is not set for operation {1}").format(
                        row.idx, row.opration_name
                    )
                )


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
            work_order.wip_warehouse = row.work_in_progress_warehouse
            work_order.fg_warehouse = row.target_warehouse
            work_order.scrap_warehouse = row.scrape_warhouse
            work_order.planned_start_date = self.posting_date or frappe.utils.now_datetime()
            work_order.insert()

            row.db_set("work_order_number", work_order.name, update_modified=False)


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
                    "production_plan_number": self.production_plan_number,
                    "qty_to_manufacture": item.planned_qty,
                    "manufacture_qty" : item.produced_qty,
                    "bom_no": item.bom_no,
                    "source_warehouse": self.source_warehouse,
                    "target_warehouse": self.target_warehouse,
                    "work_in_progress_warehouse": self.work_in_progress_warehouse,
                    "scrape_warhouse": self.scrape_warhouse,
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

        unique_workstation = set()

        # Append operations
        for op in operations:
            unique_workstation.add(op.workstation)
            self.append("operations", {
                "opration_sequence_no" : op.sequence_id,
                "opration_name": op.operation,
                "workstation": op.workstation,
                "workstation_type": op.workstation_type,
                "standerd_time": op.time_in_mins,
            })

        hour_rate_map = frappe._dict(frappe.get_all("Workstation", {"name" : ["in", unique_workstation]}, ["name", "hour_rate"], as_list=1))
        for row in self.operations:
            workstation = row.get("workstation")
            row.hour_rate = hour_rate_map.get(workstation)


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
                "uom"
            ],
        )
        return items


    def set_required_items(self, items):
        self.set("required_items", [])
        unique_items = set()

        # Append operations
        for item in items:
            unique_items.add(item.item_code)
            self.append("required_items", {
                "item_code": item.item_code,
                "item_name": item.item_name,
                "uom": item.uom,
                "requried_qty": item.qty,
                "transfer_qty" : 0,
                "return_qty" : 0,
                "consumed_qty" : 0
            })

        items_details = frappe._dict(frappe.get_all("Item", {"name":["in", unique_items]}, ["name", "valuation_rate"], as_list=1))
        for row in self.required_items:
            row.rate = items_details.get(row.item_code, 0)
            row.amount = (row.requried_qty or 0) * row.rate

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

        for item in scrap_items:
            self.append("scrap_item", {
                "item_code" : item.item_code,
                "item_name" : item.item_name,
                "uom" : item.uom,
                "scrap_qty" : item.qty,
                "scrape_warhouse" : ""
            })


@frappe.whitelist()
def make_master_work_order(production_plan_id):
    doc = frappe.new_doc("Master Work Order")
    doc.production_plan_number = production_plan_id
    return doc._make_master_work_order()
