# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import flt


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
        self.db_set("status", "Not Started")

    def on_cancel(self):
        self.db_set("status", "Cancelled")

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
            work_order.planned_start_date = self.posting_date or frappe.utils.now_datetime()

            self.set_in_house_operations(work_order)

            work_order.insert()
            work_order.submit()

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

        work_order.operations = in_house_operations


    def before_cancel(self):
        self.validate_linked_docs_cancelled()

    def validate_linked_docs_cancelled(self):
        pending = []

        for row in self.items_to_be_manufacture:
            if row.work_order_number and frappe.db.get_value(
                "Work Order", row.work_order_number, "docstatus"
            ) == 1:
                pending.append(("Work Order {0}").format(row.work_order_number))


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

    def set_planned_dates(self):
        if not self.items_to_be_manufacture:
            return

        start = self.items_to_be_manufacture[0].planned_start_date
        if not start:
            return

        self.planned_start_date = start

        total_minutes = sum(frappe.utils.flt(op.standerd_time) for op in self.operations)
        self.planned_end_date = frappe.utils.add_to_date(start, minutes=total_minutes)


    @frappe.whitelist()
    def start_job_card(self):
        self.transfer_material_for_work_orders()
        self.create_master_job_cards()
        self.db_set("actual_start_date", frappe.utils.now_datetime())
        self.db_set("status", "In Process")

    def transfer_material_for_work_orders(self):
        """Create and submit a Material Transfer for Manufacture for each linked
        Work Order using ERPNext's own logic -- this starts each Work Order."""
        from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry

        for row in self.items_to_be_manufacture:
            if not row.work_order_number:
                continue

            stock_entry = frappe.get_doc(
                make_stock_entry(row.work_order_number, "Material Transfer for Manufacture")
            )
            stock_entry.master_work_order = self.name
            stock_entry.insert()
            stock_entry.submit()

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
            master_job_card.submit()

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
    def finish_work_orders(self):
        """Finish every linked Work Order together -- ERPNext 'Manufacture' Stock
        Entry per Work Order, which produces the FG and completes each one."""
        from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry

        for row in self.items_to_be_manufacture:
            if not row.work_order_number:
                continue
            status = frappe.db.get_value("Work Order", row.work_order_number, "status")
            if status in ("Completed", "Closed", "Cancelled"):
                continue

            stock_entry = frappe.get_doc(
                make_stock_entry(row.work_order_number, "Manufacture")
            )
            stock_entry.insert()
            stock_entry.submit()

    # ------------------------------------------------------------------
    # Status -- Close / Stop / Re-open, applied to every linked Work Order
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