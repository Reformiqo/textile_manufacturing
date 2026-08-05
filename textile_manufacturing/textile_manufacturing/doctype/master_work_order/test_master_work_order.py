# Copyright (c) 2026, Reformiqo and Contributors
# See license.txt

"""End-to-end cover for the Master Work Order cycle.

Each test builds its own item, BOM, operations and Production Plan, so nothing here
depends on what happens to be on the site, and every one rolls the database back when
it finishes. Safe to run against a working site -- nothing is committed.

UnitTestCase rather than IntegrationTestCase on purpose: the integration base creates
test records for every link dependency and commits them, which on a site with real data
means a clashing Fiscal Year and test rows left behind for good.

Run them with:
    bench --site tm.localhost run-tests --module \\
        textile_manufacturing.textile_manufacturing.doctype.master_work_order.test_master_work_order
"""

import frappe
from frappe.tests import UnitTestCase
from frappe.utils import flt

ORDER_QTY = 10.0
IN_HOUSE = 2


class IntegrationTestMasterWorkOrder(UnitTestCase):
	"""Each test raises its own Production Plan and Master Work Order, then rolls back.

	Item, BOM and warehouses are borrowed from the site rather than built here: a
	manufacturing run needs valuation rates, expense accounts and warehouse accounts
	behind it, and reusing master data that already produces cleanly keeps the tests
	about this app rather than about ERPNext's accounting setup.
	"""

	# ------------------------------------------------------------------
	# Fixtures
	# ------------------------------------------------------------------
	def setUp(self):
		self.employee = self.first("Employee", {"status": "Active"})
		if not self.employee:
			self.skipTest("no active Employee to run a job card")

		reference = frappe.get_all(
			"Master Work Order",
			filters={"docstatus": 1},
			fields=["company", "source_warehouse", "wip_warehouse",
					"fg_warehouse", "scrap_warehouse"],
			order_by="creation desc",
			limit=1,
		)
		if not reference:
			self.skipTest("no submitted Master Work Order to take warehouses from")
		self.reference = reference[0]

		self.bom, self.item = self.find_bom()
		if not self.bom:
			self.skipTest("no submitted BOM with at least two operations")

		self.plan = self.make_production_plan()

	def tearDown(self):
		frappe.db.rollback()

	def first(self, doctype, filters=None):
		found = frappe.get_all(doctype, filters=filters or {}, pluck="name", limit=1)
		return found[0] if found else None

	def find_bom(self):
		"""An active BOM that runs at least two operations."""
		rows = frappe.db.sql(
			"""
			select b.name, b.item
			from `tabBOM` b
			join `tabBOM Operation` bo on bo.parent = b.name
			where b.docstatus = 1 and b.is_active = 1 and b.with_operations = 1
				and b.company = %(company)s
			group by b.name, b.item
			having count(bo.name) >= %(minimum)s
			order by b.modified desc
			limit 1
			""",
			{"company": self.reference.company, "minimum": IN_HOUSE},
			as_dict=True,
		)
		return (rows[0].name, rows[0].item) if rows else (None, None)

	def make_production_plan(self):
		plan = frappe.get_doc({
			"doctype": "Production Plan",
			"company": self.reference.company,
			"posting_date": frappe.utils.nowdate(),
			"po_items": [{
				"item_code": self.item,
				"bom_no": self.bom,
				"planned_qty": ORDER_QTY,
				"stock_uom": frappe.db.get_value("Item", self.item, "stock_uom"),
				"planned_start_date": frappe.utils.now_datetime(),
				"warehouse": self.reference.fg_warehouse,
			}],
		})
		plan.insert()
		plan.submit()
		return plan.name

	def make_order(self):
		"""A submitted Master Work Order with the first operations run in house."""
		from textile_manufacturing.textile_manufacturing.doctype.master_work_order.master_work_order import (
			make_master_work_order,
		)

		order = frappe.get_doc("Master Work Order", make_master_work_order(self.plan))
		for idx, op in enumerate(order.operations):
			op.manufacturing_type = "In-House" if idx < IN_HOUSE else "Out House"

		order.source_warehouse = self.reference.source_warehouse
		for row in order.items_to_be_manufacture:
			row.source_warehouse = self.reference.source_warehouse

		# Nothing here is about the WIP transfer, so it is skipped -- the operations
		# and the Finish are what these tests are for.
		order.skip_material_transfer_to_wip_warehouse = 1
		order.material_transfer_on = "Work Order"
		order.save()
		order.submit()
		order.reload()

		self.operations = [op.opration_name for op in order.in_house_operations()]
		self.assertEqual(len(self.operations), IN_HOUSE)
		return order

	# ------------------------------------------------------------------
	# Helpers
	# ------------------------------------------------------------------
	def cards_of(self, order):
		return frappe.get_all(
			"Master Job Card",
			filters={"master_work_order_number": order.name, "docstatus": ["<", 2]},
			fields=["name", "operation_name"],
			order_by="creation",
		)

	def run_card(self, name, completed=None, loss=0.0):
		"""Start a card and complete it, reporting against each row's own qty.

		Qty to Manufacture is held to the cap first, the way the dialog forces the
		operator to when an earlier operation has lost material."""
		card = frappe.get_doc("Master Job Card", name)

		caps = card.qty_caps()
		lowered = False
		for row in card.job_card_detail:
			cap = flt(caps.get(row.work_order_number, flt(row.qty_to_manufacture)))
			if flt(row.qty_to_manufacture) > cap:
				row.qty_to_manufacture = cap
				lowered = True
		if lowered:
			card.save()
			card = frappe.get_doc("Master Job Card", name)

		card.start_jobs(employees=[{"employee": self.employee}])
		card.reload()

		rows = []
		for row in card.job_card_detail:
			if not row.job_card_number:
				continue
			ordered = flt(row.qty_to_manufacture)
			lost = min(loss, ordered)
			made = ordered - lost if completed is None else min(completed, ordered - lost)
			rows.append({
				"job_card_number": row.job_card_number,
				"qty_to_manufacture": ordered,
				"completed_qty": made,
				"process_loss_qty": lost,
				"rejected_qty": 0.0,
				"rejection_reason": "",
			})

		card.complete_jobs(rows=rows)
		card.reload()
		return card

	def finish(self, order, qty=None):
		order.reload()
		offered = order.pending_manufacture_rows()
		if not offered:
			return []

		rows = [{
			"work_order_number": row["work_order_number"],
			"qty": row["qty"] if qty is None else min(qty, row["qty"]),
		} for row in offered]
		order.finish_work_orders(rows=rows)
		order.reload()
		return offered

	def operation_row(self, order, operation):
		return frappe.db.get_value(
			"Master Work Order Operation",
			{"parent": order.name, "opration_name": operation},
			["total_qty_to_manufacture", "completed_qty", "process_loss_qty",
			 "pending_qty", "status"],
			as_dict=True,
		)

	def assert_item_balances(self, order):
		"""Made plus lost must account for the order, and pending is never negative."""
		order.reload()
		for row in order.items_to_be_manufacture:
			made = flt(row.manufacture_qty)
			lost = flt(row.process_loss_qty)
			self.assertGreaterEqual(
				flt(row.pending_qty), 0, f"{row.item_code}: pending went negative"
			)
			self.assertAlmostEqual(
				made + lost, flt(row.qty_to_manufacture), places=3,
				msg=f"{row.item_code}: made {made} + lost {lost} does not account for "
					f"{flt(row.qty_to_manufacture)}",
			)

	# ------------------------------------------------------------------
	# 1 -- the straight run
	# ------------------------------------------------------------------
	def test_full_run_without_loss(self):
		order = self.make_order()

		cards = self.cards_of(order)
		self.assertEqual(len(cards), len(self.operations))

		work_order = order.items_to_be_manufacture[0].work_order_number
		self.assertTrue(work_order, "the order should have raised a Work Order")

		for card in cards:
			completed = self.run_card(card.name)
			self.assertEqual(completed.docstatus, 1)
			self.assertEqual(completed.status, "Completed")

		for name in self.operations:
			row = self.operation_row(order, name)
			self.assertEqual(flt(row.completed_qty), ORDER_QTY)
			self.assertEqual(flt(row.process_loss_qty), 0)
			self.assertEqual(flt(row.pending_qty), 0)
			self.assertEqual(row.status, "Completed")

		order.reload()
		self.assertFalse(
			order.show_pending_master_job_card_button(),
			"nothing is outstanding, so no pending card should be offered",
		)

		offered = self.finish(order)
		self.assertEqual(flt(offered[0]["qty"]), ORDER_QTY)

		order.reload()
		self.assertEqual(flt(order.items_to_be_manufacture[0].manufacture_qty), ORDER_QTY)
		self.assertEqual(order.status, "Completed")
		self.assertEqual(frappe.db.get_value("Work Order", work_order, "status"), "Completed")
		self.assertEqual(order.pending_manufacture_rows(), [])
		self.assert_item_balances(order)

	# ------------------------------------------------------------------
	# 2 -- half now, the rest on a pending card
	# ------------------------------------------------------------------
	def test_part_production_raises_pending_cards(self):
		order = self.make_order()
		half = ORDER_QTY / 2
		work_order = order.items_to_be_manufacture[0].work_order_number

		for card in self.cards_of(order):
			self.run_card(card.name, completed=half)

		for name in self.operations:
			row = self.operation_row(order, name)
			self.assertEqual(flt(row.completed_qty), half)
			self.assertEqual(flt(row.pending_qty), half, f"{name} should have half left")

		# The Finish books only what came off the line.
		offered = self.finish(order)
		self.assertEqual(flt(offered[0]["qty"]), half)
		order.reload()
		self.assertEqual(flt(order.items_to_be_manufacture[0].manufacture_qty), half)
		self.assertNotEqual(order.status, "Completed")

		# Now the balance.
		order.reload()
		self.assertTrue(order.show_pending_master_job_card_button())

		pending = order.pending_master_job_card_operations()
		self.assertEqual(len(pending), len(self.operations))
		for row in pending:
			self.assertEqual(flt(row["qty"]), half)

		before = {card.name for card in self.cards_of(order)}
		job_cards_before = frappe.db.count("Job Card", {"work_order": work_order})

		created = order.make_pending_master_job_cards(operations=pending)
		self.assertEqual(len(created), len(self.operations))
		self.assertFalse(before & set(created), "pending cards must be new documents")

		# Each pending card raises a Job Card of its own, for the balance alone.
		self.assertEqual(
			frappe.db.count("Job Card", {"work_order": work_order}),
			job_cards_before + len(self.operations),
		)
		for name in created:
			card = frappe.get_doc("Master Job Card", name)
			self.assertEqual(flt(card.total_qty_to_manufacture), half)
			for row in card.job_card_detail:
				self.assertEqual(
					flt(frappe.db.get_value("Job Card", row.job_card_number, "for_quantity")),
					half,
				)

		for name in created:
			self.run_card(name)

		for name in self.operations:
			row = self.operation_row(order, name)
			self.assertEqual(flt(row.completed_qty), ORDER_QTY)
			self.assertEqual(flt(row.pending_qty), 0)

		order.reload()
		self.assertFalse(order.show_pending_master_job_card_button())

		offered = self.finish(order)
		self.assertEqual(flt(offered[0]["qty"]), half)

		order.reload()
		self.assertEqual(flt(order.items_to_be_manufacture[0].manufacture_qty), ORDER_QTY)
		self.assertEqual(order.status, "Completed")
		self.assertEqual(order.pending_manufacture_rows(), [])
		self.assert_item_balances(order)

	# ------------------------------------------------------------------
	# 3 -- loss upstream is not work still to do
	# ------------------------------------------------------------------
	def test_loss_at_first_operation_offers_no_pending_card(self):
		order = self.make_order()
		first, second = self.cards_of(order)
		half = ORDER_QTY / 2

		# Half made, half destroyed: the order can never be completed in full.
		self.run_card(first.name, completed=half, loss=half)

		row = self.operation_row(order, self.operations[0])
		self.assertEqual(flt(row.completed_qty), half)
		self.assertEqual(flt(row.process_loss_qty), half)
		self.assertEqual(flt(row.pending_qty), 0)

		# The second operation can only ever run what survived.
		card = frappe.get_doc("Master Job Card", second.name)
		self.assertEqual(
			flt(card.qty_caps().get(order.items_to_be_manufacture[0].work_order_number)),
			half,
		)

		self.run_card(second.name)

		row = self.operation_row(order, self.operations[1])
		self.assertEqual(flt(row.completed_qty), half)
		self.assertEqual(
			flt(row.pending_qty), 0,
			"material lost upstream is not work this operation still has to do",
		)

		order.reload()
		self.assertFalse(
			order.show_pending_master_job_card_button(),
			"the shortfall is destroyed material, not work to be redone",
		)

		self.finish(order)
		self.assert_item_balances(order)

	# ------------------------------------------------------------------
	# 4 -- the cap holds on the server, not only in the dialog
	# ------------------------------------------------------------------
	def test_reporting_more_than_survived_is_refused(self):
		order = self.make_order()
		first, second = self.cards_of(order)

		self.run_card(first.name, completed=8.0, loss=2.0)

		card = frappe.get_doc("Master Job Card", second.name)
		card.start_jobs(employees=[{"employee": self.employee}])
		card.reload()

		rows = [{
			"job_card_number": row.job_card_number,
			"qty_to_manufacture": ORDER_QTY,   # only 8 survived
			"completed_qty": 8.0,
			"process_loss_qty": 2.0,
			"rejected_qty": 0.0,
			"rejection_reason": "",
		} for row in card.job_card_detail if row.job_card_number]

		self.assertRaises(frappe.ValidationError, card.complete_jobs, rows=rows)
