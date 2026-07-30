# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class MasterWorkOrder(Document):
    def _make_master_work_order(self):
        production_plan = frappe.get_doc(
            "Production Plan", self.production_plan_number
        )

        self.posting_date = frappe.utils.now_datetime()
        self.company = production_plan.company

        self.fetch_production_plan_items()

        bom_ids = list(
            {row.bom_no for row in self.items_to_be_manufacture if row.bom_no}
        )

        operations = self.get_operations(bom_ids)
        self.set_operations(operations)

        items = self.get_required_items(bom_ids)
        self.set_required_items(items)

        self.insert()
        return self.name

    def fetch_production_plan_items(self):
        if not self.production_plan_number:
            self.set("items_to_be_manufacture", [])
            return

        production_plan = frappe.get_doc(
            "Production Plan", self.production_plan_number
        )

        self.set("items_to_be_manufacture", [])

        for item in production_plan.po_items:
            self.append(
                "items_to_be_manufacture",
                {
                    "item_code": item.item_code,
                    "production_plan_number": self.production_plan_number,
                    "qty_to_manufacture": item.planned_qty,
                    "bom_no": item.bom_no,
                    "source_warehouse": self.source_warehouse,
                    "target_warehouse": self.target_warehouse,
                    "work_in_progress_warehouse": self.work_in_progress_warehouse,
                    "scrape_warhouse": self.scrape_warhouse,
                },
            )


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

        # Append operations
        for op in operations:
            self.append("operations", {
                "opration_name": op.operation,
                "workstation": op.workstation,
                "workstation_type": op.workstation_type,
                "standerd_time": op.time_in_mins,
            })


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

        # Append operations
        for item in items:
            self.append("required_items", {
                "item_code": item.item_code,
                "item_name": item.item_name,
                "uom": item.uom,
                "requried_qty": item.qty,
                "transfer_qty" : 0,
                "return_qty" : 0,
                "consumed_qty" : 0
            })


@frappe.whitelist()
def make_master_work_order(production_plan_id):
    doc = frappe.new_doc("Master Work Order")
    doc.production_plan_number = production_plan_id
    doc._make_master_work_order()
