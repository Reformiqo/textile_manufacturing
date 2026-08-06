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

		boms = self.find_boms()
		if not boms:
			self.skipTest("no submitted BOM with at least two operations")
		self.bom, self.item = boms[0]

		self.plan = self.make_production_plan()

	def tearDown(self):
		frappe.db.rollback()

	def first(self, doctype, filters=None):
		found = frappe.get_all(doctype, filters=filters or {}, pluck="name", limit=1)
		return found[0] if found else None

	def find_boms(self, count=1):
		"""Active BOMs sharing one at-least-two-operation routing, one per item.

		The same routing, because the order's operations table is the union over
		its items -- two items with different routings would leave operations that
		only one of them runs, and the tests' per-operation arithmetic with it.
		One per item, because the Production Plan merges rows of the same item."""
		rows = frappe.db.sql(
			"""
			select b.name, b.item,
				group_concat(bo.operation order by bo.idx) as routing
			from `tabBOM` b
			join `tabBOM Operation` bo on bo.parent = b.name
			where b.docstatus = 1 and b.is_active = 1 and b.with_operations = 1
				and b.company = %(company)s
			group by b.name, b.item
			having count(bo.name) >= %(minimum)s
			order by b.modified desc
			""",
			{"company": self.reference.company, "minimum": IN_HOUSE},
			as_dict=True,
		)

		by_routing = {}
		for row in rows:
			by_routing.setdefault(row.routing, {}).setdefault(row.item, row.name)

		for group in by_routing.values():
			if len(group) >= count:
				return [(name, item) for item, name in list(group.items())[:count]]

		return []

	def make_production_plan(self, boms=None):
		plan = frappe.get_doc({
			"doctype": "Production Plan",
			"company": self.reference.company,
			"posting_date": frappe.utils.nowdate(),
			"po_items": [{
				"item_code": item,
				"bom_no": bom,
				"planned_qty": ORDER_QTY,
				"stock_uom": frappe.db.get_value("Item", item, "stock_uom"),
				"planned_start_date": frappe.utils.now_datetime(),
				"warehouse": self.reference.fg_warehouse,
			} for bom, item in (boms or [(self.bom, self.item)])],
		})
		plan.insert()
		plan.submit()
		return plan.name

	def make_order(self, plan=None):
		"""A submitted Master Work Order with the first operations run in house."""
		from textile_manufacturing.textile_manufacturing.doctype.master_work_order.master_work_order import (
			make_master_work_order,
		)

		order = frappe.get_doc("Master Work Order", make_master_work_order(plan or self.plan))
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

		# The order the tests drive the cards in, and nothing more. No routing is
		# read anywhere -- the floor runs the operations in whatever order it
		# likes, and the arithmetic under test settles everything off the reported
		# quantities. self.operations[0] simply means "the one driven first".
		self.operations = [op.opration_name for op in order.in_house_operations()]
		self.assertEqual(len(self.operations), IN_HOUSE)
		return order

	def two_item_order(self):
		"""A submitted order for two items of 10, both on the same routing.

		Two items because that is where the ledgers come apart: the operation rows
		carry both added together while the item rows and Work Orders stay
		separate, and a figure written to the wrong one still looks right on a
		single-item order."""
		boms = self.find_boms(count=2)
		if len(boms) < 2:
			self.skipTest("no two BOMs sharing the same two-operation routing")

		order = self.make_order(plan=self.make_production_plan(boms=boms))
		self.assertEqual(len(order.items_to_be_manufacture), 2)
		return order

	# ------------------------------------------------------------------
	# Helpers
	# ------------------------------------------------------------------
	def cards_of(self, order):
		"""Every card on the order, matched to the order the tests drive them in."""
		cards = frappe.get_all(
			"Master Job Card",
			filters={"master_work_order_number": order.name, "docstatus": ["<", 2]},
			fields=["name", "operation_name"],
			order_by="creation",
		)
		position = {name: idx for idx, name in enumerate(self.operations)}
		return sorted(
			cards, key=lambda card: position.get(card.operation_name, len(position))
		)

	def run_card(self, name, completed=None, loss=0.0, rejected=0.0, qty=None):
		"""Start a card and complete it, reporting against each row's own qty.

		qty stands in for the operator typing a smaller Qty to Manufacture into the
		dialog -- a run of 5 off an order for 10. Whatever it is left at, it is held
		to the cap first, the way the dialog forces the operator to when an earlier
		operation has lost material."""
		card = frappe.get_doc("Master Job Card", name)

		caps = card.qty_caps()
		lowered = False
		for row in card.job_card_detail:
			if qty is not None and flt(row.qty_to_manufacture) != flt(qty):
				row.qty_to_manufacture = flt(qty)
				lowered = True

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
			bad = min(rejected, ordered - lost)
			room = ordered - lost - bad
			made = room if completed is None else min(completed, room)
			rows.append({
				"job_card_number": row.job_card_number,
				"qty_to_manufacture": ordered,
				"completed_qty": made,
				"process_loss_qty": lost,
				"rejected_qty": bad,
				"rejection_reason": "Failed inspection" if bad else "",
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
			 "pending_qty", "status", "actual_time", "hour_rate", "operating_cost",
			 "manufacturing_type"],
			as_dict=True,
		)

	# ------------------------------------------------------------------
	# Assertions -- every field the cycle writes, and every button it governs
	# ------------------------------------------------------------------
	def buttons(self, order):
		"""What the form would draw, read the way the form reads it.

		Both buttons are decided in onload and nowhere else, so this asks onload
		rather than re-deriving the answer -- a test that called the underlying
		method directly would pass while the button itself stayed wrong."""
		doc = frappe.get_doc("Master Work Order", order.name)
		doc.onload()
		onload = doc.get("__onload") or frappe._dict()

		return frappe._dict({
			# add_finish_button(): drawn whenever onload offers any row at all.
			"finish": bool(onload.get("pending_manufacture_rows")),
			# add_pending_master_job_card_button(): the flag, and the form also
			# requires the operations table to be non-empty.
			"pending_card": bool(onload.get("show_pending_master_job_card_button"))
				and bool(doc.operations),
			# add_status_buttons(): Close and Stop go once the order is finished.
			"close_stop": doc.status not in ("Completed", "Cancelled", "Closed"),
			"blocked_reason": onload.get("finish_blocked_reason"),
			"rows": onload.get("pending_manufacture_rows") or [],
			"operations": onload.get("pending_master_job_card_operations") or [],
		})

	def assert_buttons(self, order, finish, pending_card, close_stop=True, when=""):
		actual = self.buttons(order)
		self.assertEqual(actual.finish, finish, f"{when}: Finish button")
		self.assertEqual(
			actual.pending_card, pending_card, f"{when}: Pending Master Job Card button"
		)
		self.assertEqual(actual.close_stop, close_stop, f"{when}: Close/Stop buttons")

	def assert_operation(self, order, operation, completed, lost, pending, status,
						 ordered=ORDER_QTY, when=""):
		"""Every figure the operation row carries, and the cost derived from them."""
		row = self.operation_row(order, operation)
		where = f"{when}: operation {operation}"

		self.assertEqual(row.manufacturing_type, "In-House", f"{where}: manufacturing type")
		self.assertAlmostEqual(
			flt(row.total_qty_to_manufacture), ordered, places=3,
			msg=f"{where}: Total Qty to Manufacture must keep the order's own figure",
		)
		self.assertAlmostEqual(flt(row.completed_qty), completed, places=3,
			msg=f"{where}: completed qty")
		self.assertAlmostEqual(flt(row.process_loss_qty), lost, places=3,
			msg=f"{where}: process loss qty (process loss and rejects together)")
		self.assertAlmostEqual(flt(row.pending_qty), pending, places=3,
			msg=f"{where}: pending qty")
		self.assertEqual(row.status, status, f"{where}: status")

		# The law every operation row obeys, whatever the scenario: what it made,
		# what was lost to it and what it still owes account for what it was asked
		# for. A row that does not add up is a row nobody can check.
		self.assertAlmostEqual(
			flt(row.completed_qty) + flt(row.process_loss_qty) + flt(row.pending_qty),
			flt(row.total_qty_to_manufacture), places=3,
			msg=f"{where}: completed {flt(row.completed_qty)} + loss "
				f"{flt(row.process_loss_qty)} + pending {flt(row.pending_qty)} does "
				f"not account for {flt(row.total_qty_to_manufacture)}",
		)
		self.assertGreaterEqual(flt(row.pending_qty), 0, f"{where}: pending negative")
		self.assertGreaterEqual(flt(row.process_loss_qty), 0, f"{where}: loss negative")

		# Time is real elapsed time, so only its shape can be asserted -- but the
		# cost must always be the row's own two figures multiplied out.
		self.assertGreaterEqual(flt(row.actual_time), 0, f"{where}: actual time")
		self.assertAlmostEqual(
			flt(row.operating_cost),
			flt((flt(row.actual_time) / 60.0) * flt(row.hour_rate), 2),
			places=2,
			msg=f"{where}: operating cost must be actual time / 60 x hour rate",
		)

	def assert_untouched_operations(self, order, when=""):
		"""The Out House rows the order also carries must stay at nothing.

		They are on the same table as the In-House ones, and a sync that wrote to
		the wrong row would otherwise go unnoticed."""
		for op in order.operations:
			if op.manufacturing_type == "In-House":
				continue
			row = self.operation_row(order, op.opration_name)
			where = f"{when}: out-house operation {op.opration_name}"
			self.assertAlmostEqual(flt(row.completed_qty), 0, places=3, msg=where)
			self.assertAlmostEqual(flt(row.process_loss_qty), 0, places=3, msg=where)
			self.assertAlmostEqual(flt(row.pending_qty), 0, places=3, msg=where)

	def assert_items(self, order, made, lost, pending, status, when=""):
		"""Every item row, and the Work Order behind each of them.

		The same figures are kept in three places -- the item row, the Work Order
		and the order's own totals -- and the bug this guards against is exactly
		one of them disagreeing."""
		order.reload()
		self.assertTrue(order.items_to_be_manufacture, f"{when}: order has no items")

		for row in order.items_to_be_manufacture:
			where = f"{when}: item {row.item_code}"

			self.assertTrue(row.work_order_number, f"{where}: no Work Order raised")
			self.assertTrue(row.bom_no, f"{where}: no BOM")
			self.assertEqual(
				row.production_plan_number, order.production_plan_number,
				f"{where}: Production Plan link",
			)
			self.assertAlmostEqual(flt(row.qty_to_manufacture), ORDER_QTY, places=3,
				msg=f"{where}: Qty to Manufacture must never move")
			self.assertAlmostEqual(flt(row.manufacture_qty), made, places=3,
				msg=f"{where}: Manufacture Qty")
			self.assertAlmostEqual(flt(row.process_loss_qty), lost, places=3,
				msg=f"{where}: Process Loss Qty")
			self.assertAlmostEqual(flt(row.pending_qty), pending, places=3,
				msg=f"{where}: Pending Qty")
			self.assertEqual(row.status, status, f"{where}: status")

			# The same law on the item row: made, lost and still to come account
			# for what was ordered.
			self.assertGreaterEqual(flt(row.pending_qty), 0, f"{where}: pending negative")
			self.assertAlmostEqual(
				flt(row.manufacture_qty) + flt(row.process_loss_qty)
				+ flt(row.pending_qty),
				ORDER_QTY, places=3,
				msg=f"{where}: made {flt(row.manufacture_qty)} + lost "
					f"{flt(row.process_loss_qty)} + pending {flt(row.pending_qty)} "
					f"does not account for {ORDER_QTY}",
			)

			work_order = frappe.db.get_value(
				"Work Order", row.work_order_number,
				["status", "qty", "produced_qty", "process_loss_qty"], as_dict=True,
			)
			self.assertAlmostEqual(flt(work_order.qty), ORDER_QTY, places=3,
				msg=f"{where}: Work Order qty")
			self.assertAlmostEqual(flt(work_order.produced_qty), made, places=3,
				msg=f"{where}: Work Order produced qty must match the item row")
			self.assertAlmostEqual(
				flt(work_order.process_loss_qty), lost, places=3,
				msg=f"{where}: the Work Order must carry the loss combined over every "
					f"operation, not the highest single one",
			)
			# The Work Order's own rule, and the one that decides the order's status:
			# Completed exactly when produced plus lost accounts for the quantity.
			expected = ("Completed"
				if flt(work_order.produced_qty) + flt(work_order.process_loss_qty)
					>= ORDER_QTY - 0.001
				else "In Process")
			self.assertEqual(work_order.status, expected, f"{where}: Work Order status")
			self.assertEqual(
				row.status, order.item_status(work_order.status),
				f"{where}: the item row's status must follow the Work Order's",
			)

	def assert_order(self, order, status, made, lost, when=""):
		"""The order's own header figures, and its links onward."""
		order.reload()
		where = f"{when}: order"

		count = len(order.items_to_be_manufacture)
		self.assertEqual(order.status, status, f"{where}: status")
		self.assertAlmostEqual(
			flt(order.total_manufacture_qty), made * count, places=3,
			msg=f"{where}: Total Manufacture Qty must be the item rows summed",
		)
		self.assertAlmostEqual(
			flt(order.total_process_loss), lost * count, places=3,
			msg=f"{where}: Total Process Loss must be the item rows summed",
		)
		self.assertAlmostEqual(
			flt(order.total_qty_to_manufacture), ORDER_QTY * count, places=3,
			msg=f"{where}: Total Qty to Manufacture",
		)
		self.assertTrue(order.actual_start_date, f"{where}: actual start date")

		if status == "Completed":
			self.assertTrue(
				order.actual_end_date,
				f"{where}: a completed order must be stamped with an end date",
			)
			self.assertEqual(
				order.pending_manufacture_rows(), [],
				f"{where}: a completed order has nothing left to finish",
			)

		# Costs are only ever booked off submitted Manufacture entries.
		if made > 0:
			self.assertGreater(
				flt(order.total_raw_material_cost), 0,
				f"{where}: goods were produced, so raw material cost must be booked",
			)
		else:
			self.assertAlmostEqual(flt(order.total_raw_material_cost), 0, places=2,
				msg=f"{where}: nothing produced, so no raw material cost")

	def assert_cards_completed(self, order, count=None, when=""):
		"""Every Master Job Card raised is submitted and Completed."""
		cards = frappe.get_all(
			"Master Job Card",
			filters={"master_work_order_number": order.name, "docstatus": ["<", 2]},
			fields=["name", "status", "docstatus"],
		)
		if count is not None:
			self.assertEqual(len(cards), count, f"{when}: number of Master Job Cards")
		for card in cards:
			self.assertEqual(card.docstatus, 1, f"{when}: {card.name} not submitted")
			self.assertEqual(card.status, "Completed", f"{when}: {card.name} status")

	# ------------------------------------------------------------------
	# 1 -- rejection at both operations, two items, and the Finish closes it
	# ------------------------------------------------------------------
	def test_rejection_at_both_operations_completes_both_items(self):
		"""Two items of 10; the first operation rejects 5 of each, the second 1
		more. The Finish must then square every ledger at once: the item rows, the
		operation rows, the Work Orders and the order's own status.

		The trap is in the Work Order: ERPNext holds its loss to the highest single
		operation's (5), which leaves it a piece short of Completed for good -- the
		loss is 6, spread 5 and 1 over two operations, and the order can then never
		leave In Process."""
		order = self.two_item_order()

		first, second = self.cards_of(order)
		self.run_card(first.name, rejected=5.0)
		# The second card is held to the 5 that survived; 1 more dies there.
		self.run_card(second.name, rejected=1.0)

		self.assert_cards_completed(order, count=2, when="both operations run")

		# Two items, so the operation rows carry both added together. Each row is
		# its own sums: the first ran all 20, 10 out and 10 rejected, and is
		# through. The second ran the 10 that reached it, 8 out and 2 rejected, so
		# against its 20 it still reads 10 pending.
		self.assert_operation(order, self.operations[0], completed=10.0, lost=10.0,
			pending=0.0, status="Completed", ordered=20.0, when="rejected at both")
		self.assert_operation(order, self.operations[1], completed=8.0, lost=12.0,
			pending=0.0, status="Completed", ordered=20.0, when="rejected at both")
		self.assert_untouched_operations(order, when="rejected at both")

		# No cloth left -- 4 apiece off the line and 6 apiece destroyed accounts for
		# the order -- so no card is offered even though the second row reads 10.
		self.assert_buttons(order, finish=True, pending_card=False,
			when="before the Finish")
		self.assertEqual(order.outstanding_after_loss(), {},
			"made plus destroyed accounts for the order -- nothing left to run")
		self.assertIsNone(
			self.buttons(order).blocked_reason,
			"the line has turned goods out, so the Finish must not be blocked",
		)

		self.assert_items(order, made=0.0, lost=6.0, pending=4.0, status="In Process",
			when="before the Finish")

		offered = self.finish(order)
		self.assertEqual([flt(row["qty"]) for row in offered], [4.0, 4.0])
		# The dialog shows what the order really lost -- 5 rejected at one
		# operation and 1 at the next -- not ERPNext's highest-single-operation 5.
		self.assertEqual([flt(row["process_loss_qty"]) for row in offered], [6.0, 6.0])
		self.assertEqual([flt(row["qty_to_manufacture"]) for row in offered], [10.0, 10.0])

		self.assert_items(order, made=4.0, lost=6.0, pending=0.0, status="Completed",
			when="after the Finish")
		self.assert_order(order, status="Completed", made=4.0, lost=6.0,
			when="after the Finish")
		# Nothing left to press: the order is closed to further work.
		self.assert_buttons(order, finish=False, pending_card=False, close_stop=False,
			when="after the Finish")

	# ------------------------------------------------------------------
	# 2 -- the straight run: nothing partial, nothing lost
	# ------------------------------------------------------------------
	def test_full_run_without_loss(self):
		"""Two items of 10 straight through both operations. Every figure ends at
		the quantity ordered, every status at Completed, and neither the pending
		card nor the Finish is left offered."""
		order = self.two_item_order()

		cards = self.cards_of(order)
		self.assertEqual(len(cards), len(self.operations))

		for card in cards:
			completed = self.run_card(card.name)
			self.assertEqual(completed.docstatus, 1)
			self.assertEqual(completed.status, "Completed")
			self.assertAlmostEqual(flt(completed.total_completed_qty), 20.0, places=3)
			self.assertAlmostEqual(flt(completed.total_process_loss_qty), 0.0, places=3)
			self.assertAlmostEqual(flt(completed.total_rejected_qty), 0.0, places=3)

		self.assert_cards_completed(order, count=2, when="both operations run")

		for name in self.operations:
			self.assert_operation(order, name, completed=20.0, lost=0.0, pending=0.0,
				status="Completed", ordered=20.0, when="run in full")
		self.assert_untouched_operations(order, when="run in full")

		self.assert_buttons(order, finish=True, pending_card=False,
			when="before the Finish")
		self.assert_items(order, made=0.0, lost=0.0, pending=10.0, status="In Process",
			when="before the Finish")

		offered = self.finish(order)
		self.assertEqual([flt(row["qty"]) for row in offered], [ORDER_QTY, ORDER_QTY])

		self.assert_items(order, made=10.0, lost=0.0, pending=0.0, status="Completed",
			when="after the Finish")
		self.assert_order(order, status="Completed", made=10.0, lost=0.0,
			when="after the Finish")
		self.assert_buttons(order, finish=False, pending_card=False, close_stop=False,
			when="after the Finish")

	# ------------------------------------------------------------------
	# 3 -- part runs with loss, twice over, then the Finish
	# ------------------------------------------------------------------
	def test_part_runs_with_loss_then_pending_cards_then_finish(self):
		"""Two items of 10, run 5 at a time with loss at each pass.

		The operator types 5 into Qty to Manufacture rather than the 10 the card was
		raised for, and 1 of the 5 is lost. What that leaves outstanding is the point
		of the test: not 5, because a lost piece is not waiting to be made, and not
		measured against the card either -- against the order, less everything made
		and everything lost anywhere. The second pass repeats it, and the Finish is
		held throughout to what has cleared both operations less what is already
		booked."""
		order = self.two_item_order()
		first, second = self.cards_of(order)

		# Pass one: 5 put through the first operation, 1 lost; the second then has
		# only the 4 survivors to work.
		self.run_card(first.name, qty=5.0, loss=1.0)
		self.run_card(second.name, qty=4.0)

		# The partial case, and the one where Pending has to carry a real figure.
		# Each row off its own sums against its 20: the first ran 10 (8 out, 2
		# destroyed) so it owes 10; the second ran 8 and destroyed none, so it
		# owes 12.
		self.assert_operation(order, self.operations[0], completed=8.0, lost=2.0,
			pending=10.0, status="Completed", ordered=20.0, when="after pass one")
		self.assert_operation(order, self.operations[1], completed=8.0, lost=2.0,
			pending=10.0, status="Completed", ordered=20.0, when="after pass one")

		# Both buttons stand: work is outstanding and goods are ready to book.
		self.assert_buttons(order, finish=True, pending_card=True,
			when="after pass one")
		self.assert_items(order, made=0.0, lost=1.0, pending=9.0, status="In Process",
			when="after pass one")

		offered = self.finish(order)
		self.assertEqual([flt(row["qty"]) for row in offered], [4.0, 4.0])
		self.assert_items(order, made=4.0, lost=1.0, pending=5.0, status="In Process",
			when="after the first Finish")
		self.assert_order(order, status="In Process", made=4.0, lost=1.0,
			when="after the first Finish")

		# Pass two: each operation is offered exactly what its own row says it owes.
		# This is the partial case working -- unfinished work is pending and
		# offered, where destroyed cloth is neither.
		pending = {row["opration_name"]: flt(row["qty"])
				   for row in order.pending_master_job_card_operations()}
		self.assertEqual(pending, {
			self.operations[0]: 10.0,
			self.operations[1]: 10.0,
		})

		created = order.make_pending_master_job_cards(
			operations=order.pending_master_job_card_operations()
		)
		self.assertEqual(len(created), len(self.operations))

		# The cards come back in the operations table's order, so they are matched
		# to their operation by name rather than by position.
		by_operation = {
			frappe.db.get_value("Master Job Card", name, "operation_name"): name
			for name in created
		}

		# 1 more destroyed at the first operation, so 4 reach the second again.
		self.run_card(by_operation[self.operations[0]], qty=5.0, loss=1.0)
		self.run_card(by_operation[self.operations[1]], qty=4.0)

		self.assert_cards_completed(order, count=4, when="after pass two")

		# The first has now run all 20 -- 16 out, 4 destroyed -- and is through. The
		# second ran the 16 that reached it and destroyed none, so its row still
		# reads 4 against its 20.
		self.assert_operation(order, self.operations[0], completed=16.0, lost=4.0,
			pending=0.0, status="Completed", ordered=20.0, when="after pass two")
		self.assert_operation(order, self.operations[1], completed=16.0, lost=4.0,
			pending=0.0, status="Completed", ordered=20.0, when="after pass two")
		self.assert_untouched_operations(order, when="after pass two")

		# No cloth left -- 8 apiece off the line and 2 apiece destroyed accounts for
		# the order -- so the partial offer is gone.
		self.assert_buttons(order, finish=True, pending_card=False,
			when="after pass two")
		self.assertEqual(order.outstanding_after_loss(), {}, "nothing left to run")

		# 8 have cleared the line and 4 are booked, so the Finish offers the other 4.
		offered = self.finish(order)
		self.assertEqual([flt(row["qty"]) for row in offered], [4.0, 4.0])

		self.assert_items(order, made=8.0, lost=2.0, pending=0.0, status="Completed",
			when="after the second Finish")
		self.assert_order(order, status="Completed", made=8.0, lost=2.0,
			when="after the second Finish")
		self.assert_buttons(order, finish=False, pending_card=False, close_stop=False,
			when="after the second Finish")

	# ------------------------------------------------------------------
	# 4 -- half destroyed, across two operations
	# ------------------------------------------------------------------
	def test_half_lost_across_operations_finishes_the_other_half(self):
		"""Two items of 10; 3 die at the first operation and 2 at the second.

		Half the order is gone, and the half that is gone must not be mistaken for
		work outstanding: no pending card may be offered once the operations are
		through, and the Finish must offer 5 -- not the 7 the first operation turned
		out, and not the 10 the order asked for. The order still completes, because
		made plus lost accounts for it."""
		order = self.two_item_order()
		first, second = self.cards_of(order)

		self.run_card(first.name, loss=3.0)
		# Only the 7 survivors reach the second operation, and 2 die there.
		self.run_card(second.name, loss=2.0)

		self.assert_cards_completed(order, count=2, when="half destroyed")

		# The first ran all 20, 14 out and 6 destroyed, and is through. The second
		# ran the 14 that reached it, 10 out and 4 destroyed, so against its 20 it
		# reads 6 pending.
		self.assert_operation(order, self.operations[0], completed=14.0, lost=6.0,
			pending=0.0, status="Completed", ordered=20.0, when="half destroyed")
		self.assert_operation(order, self.operations[1], completed=10.0, lost=10.0,
			pending=0.0, status="Completed", ordered=20.0, when="half destroyed")
		self.assert_untouched_operations(order, when="half destroyed")

		# The heart of it: with half the order destroyed there is no cloth left to
		# run, so no partial entry is offered. The shortfall is destroyed material,
		# and the Finish is what closes it -- not another card.
		self.assert_buttons(order, finish=True, pending_card=False,
			when="half destroyed")
		self.assertEqual(
			order.pending_master_job_card_operations(), [],
			"nothing may be offered on a pending card -- the shortfall is destroyed",
		)
		self.assertEqual(order.outstanding_after_loss(), {},
			"5 apiece off the line and 5 apiece destroyed accounts for the order")

		self.assert_items(order, made=0.0, lost=5.0, pending=5.0, status="In Process",
			when="before the Finish")

		# Held to what cleared both operations -- 5, not the 7 the first turned out.
		offered = self.finish(order)
		self.assertEqual([flt(row["qty"]) for row in offered], [5.0, 5.0])

		self.assert_items(order, made=5.0, lost=5.0, pending=0.0, status="Completed",
			when="after the Finish")
		self.assert_order(order, status="Completed", made=5.0, lost=5.0,
			when="after the Finish")
		self.assert_buttons(order, finish=False, pending_card=False, close_stop=False,
			when="after the Finish")

	# ------------------------------------------------------------------
	# 5 -- the cap holds on the server, not only in the dialog
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

	# ------------------------------------------------------------------
	# 6 -- the Finish and the pending cards interleaved, part by part
	# ------------------------------------------------------------------
	def test_partial_finish_interleaved_with_partial_pending_cards(self):
		"""Neither ledger waits for the other.

		6 of 10 come off the line and only 3 are booked as finished; the other 4 go
		onto pending cards that themselves run only 2 before the next Finish. The
		Finish is held to what the line has turned out less what is already booked,
		the pending offer to what no card has run yet, and the two must stay right
		through every interleaving."""
		order = self.make_order()
		work_order = order.items_to_be_manufacture[0].work_order_number

		# Round one: 6 of 10 through every operation.
		for card in self.cards_of(order):
			self.run_card(card.name, completed=6.0)

		# Book 3 of the 6 -- a partial Finish of a partial run.
		offered = self.finish(order, qty=3.0)
		self.assertEqual(flt(offered[0]["qty"]), 6.0, "the line has turned out 6")
		order.reload()
		self.assertEqual(flt(order.items_to_be_manufacture[0].manufacture_qty), 3.0)

		# The other 4 are work still to do, not loss: offered on pending cards.
		pending = order.pending_master_job_card_operations()
		self.assertEqual(
			[flt(row["qty"]) for row in pending], [4.0] * len(self.operations)
		)
		created = order.make_pending_master_job_cards(operations=pending)

		# Round two: the pending cards themselves run only 2 of their 4.
		for name in created:
			self.run_card(name, completed=2.0)

		# The line has turned out 8 and 3 are booked -- the Finish may book 5 more.
		order.reload()
		self.assertEqual(
			flt(order.pending_manufacture_by_work_order().get(work_order)), 5.0
		)
		self.finish(order)
		order.reload()
		self.assertEqual(flt(order.items_to_be_manufacture[0].manufacture_qty), 8.0)

		# And 2 are still work to do, on a second round of pending cards.
		pending = order.pending_master_job_card_operations()
		self.assertEqual(
			[flt(row["qty"]) for row in pending], [2.0] * len(self.operations)
		)
		for name in order.make_pending_master_job_cards(operations=pending):
			self.run_card(name)

		order.reload()
		self.assertFalse(order.show_pending_master_job_card_button())

		self.finish(order)
		order.reload()
		self.assertEqual(
			flt(order.items_to_be_manufacture[0].manufacture_qty), ORDER_QTY
		)
		self.assertEqual(order.status, "Completed")
		self.assertEqual(order.pending_manufacture_rows(), [])

	def test_subcontracted_po_carries_every_item_to_the_supplier(self):
		"""Every item goes out, not just the one that happened to disagree with itself.

		ERPNext maps a Purchase Order row onto a Subcontracting Order only where
		qty != subcontracted_qty, and populate_items_table() then sends the supplier
		qty - subcontracted_qty. That field is its own running count of what has
		already been ordered out, kept by update_subcontracted_quantity_in_po() as
		each order is submitted, so the rows have to leave here at nothing.

		Filled in with the qty to manufacture, every row read as already
		subcontracted: an order whose figures matched was refused outright as fully
		subcontracted, and one whose service qty had been typed down to something
		else lost every row but that one -- which then went out for the difference
		between the two, a negative quantity."""
		order = self.two_item_order()

		purchase_order = order.make_subcontracted_purchase_order()

		self.assertEqual(
			len(purchase_order.items),
			len(order.items_to_be_manufacture),
			"one Purchase Order row per item to be manufactured",
		)

		for row, item in zip(purchase_order.items, order.items_to_be_manufacture):
			where = item.item_code
			self.assertEqual(row.fg_item, item.item_code, f"{where}: finished good")
			self.assertAlmostEqual(
				flt(row.fg_item_qty), flt(item.qty_to_manufacture), places=3,
				msg=f"{where}: finished goods qty",
			)
			# One unit of the operation is bought per unit made, so the service line
			# matches it -- ERPNext divides the two for the row's conversion factor.
			self.assertAlmostEqual(
				flt(row.qty), flt(item.qty_to_manufacture), places=3,
				msg=f"{where}: the service line's own qty",
			)
			self.assertAlmostEqual(
				flt(row.subcontracted_qty), 0.0, places=3,
				msg=f"{where}: nothing has been subcontracted yet -- this is ERPNext's "
					"count, and it keeps it itself",
			)
			# The mapping's own test, put to the row it will be put to.
			self.assertNotEqual(
				flt(row.qty), flt(row.subcontracted_qty),
				f"{where}: this row would be dropped from the Subcontracting Order",
			)

	def test_a_card_can_be_run_with_nobody_named_on_it(self):
		"""An operator is optional, and the run still has to add up without one.

		ERPNext writes a Job Card Time Log per employee it is handed and none at all
		for an empty list, and a Job Card with no time logs cannot be submitted
		(validate_time_logs_present) and reads as nothing made. So a card started with
		nobody named needs a row of its own -- unattributed, but there -- or the whole
		run is lost at the end of it."""
		order = self.make_order()
		card = frappe.get_doc("Master Job Card", self.cards_of(order)[0].name)

		card.start_jobs(employees=[])
		card.reload()

		self.assertTrue(
			[log for log in card.time_log if log.from_time],
			"the run has to be timed whether or not anyone is named on it",
		)
		for detail in card.job_card_detail:
			if not detail.job_card_number:
				continue
			job_card = frappe.get_doc("Job Card", detail.job_card_number)
			self.assertTrue(
				job_card.time_logs,
				f"{job_card.name}: ERPNext will not submit a Job Card with no time logs",
			)

		rows = [{
			"job_card_number": detail.job_card_number,
			"qty_to_manufacture": flt(detail.qty_to_manufacture),
			"completed_qty": flt(detail.qty_to_manufacture),
			"process_loss_qty": 0.0,
			"rejected_qty": 0.0,
			"rejection_reason": "",
		} for detail in card.job_card_detail if detail.job_card_number]

		card.complete_jobs(rows=rows)
		card.reload()

		self.assertEqual(card.docstatus, 1, "the card has to submit with no operator on it")
		self.assertAlmostEqual(
			flt(card.total_completed_qty), ORDER_QTY, places=3,
			msg="what was made is booked whether or not anyone was named for it",
		)

	def test_cutting_the_ordered_qty_cuts_what_goes_to_the_supplier(self):
		"""The finished goods qty follows the qty ordered, so the order sent out is
		the order that was placed.

		ERPNext sizes the Subcontracting Order by dividing the two figures on the row
		-- conversion_factor = qty / fg_item_qty -- and raises it for
		available qty / conversion factor. Only qty is on the items grid, so a row cut
		from 10 to 1 used to leave fg_item_qty at 10: a factor of 0.1, and finished
		goods back at 10 on the Subcontracting Order."""
		from textile_manufacturing.override.purchase_order import keep_fg_qty_in_step

		order = self.two_item_order()
		purchase_order = order.make_subcontracted_purchase_order()

		cut, kept = purchase_order.items[0], purchase_order.items[1]
		ordered = flt(kept.qty)
		cut.qty = 1.0

		keep_fg_qty_in_step(purchase_order)

		self.assertAlmostEqual(
			flt(cut.fg_item_qty), 1.0, places=3,
			msg="the finished goods qty has to come down with the qty ordered",
		)
		self.assertAlmostEqual(
			flt(cut.qty) / flt(cut.fg_item_qty), 1.0, places=3,
			msg="a conversion factor of anything but 1 scales the Subcontracting Order",
		)
		self.assertAlmostEqual(
			flt(kept.fg_item_qty), ordered, places=3,
			msg="the row that was not touched keeps what it was raised for",
		)

	def test_ordinary_subcontracting_purchase_orders_are_left_alone(self):
		"""Only this app's own orders are held to one unit of service per unit made.

		Elsewhere the service line and the finished goods are genuinely different
		quantities, and the ratio between them is the whole point of the row."""
		from textile_manufacturing.override.purchase_order import keep_fg_qty_in_step

		purchase_order = frappe.new_doc("Purchase Order")
		purchase_order.is_subcontracted = 1
		purchase_order.append("items", {"qty": 1.0, "fg_item_qty": 10.0})

		keep_fg_qty_in_step(purchase_order)

		self.assertAlmostEqual(
			flt(purchase_order.items[0].fg_item_qty), 10.0, places=3,
			msg="a Purchase Order with no Master Work Order behind it is not ours to touch",
		)


class TestPendingArithmetic(UnitTestCase):
	"""Each operation owes the order's qty less what it has itself handled.

	Handled means completed and destroyed together -- a piece it rejected is a
	piece it worked and will not work again. Nothing else enters: no routing, no
	sequence, and never what another operation did. The floor runs the operations
	in whatever order it likes, so one operation's loss is never charged to
	another.

	Built in memory with only the balances stubbed: the arithmetic is the whole of
	the question and needs no site to answer. Folding covers only WO-A throughout,
	as an operation that not every item runs."""

	def make_order(self, balances):
		order = frappe.new_doc("Master Work Order")
		for name in ("Folding", "Embroidery"):
			order.append("operations", {
				"opration_name": name,
				"manufacturing_type": "In-House",
			})
		for work_order in ("WO-A", "WO-B"):
			order.append("items_to_be_manufacture", {
				"work_order_number": work_order,
				"qty_to_manufacture": ORDER_QTY,
			})

		order.operation_balances = lambda: balances
		return order

	def figures(self, order):
		"""Every operation's figures, with the law checked on each of them.

		Completed, lost and pending must account for the order's qty on every row
		of every scenario -- not just the one a test is about. Any test that reads
		figures goes through here, so a rule that balances in one case and not
		another cannot pass."""
		figures = order.operation_figures()

		for name, by_work_order in figures.items():
			for work_order, figure in by_work_order.items():
				where = f"{name} / {work_order}"
				self.assertAlmostEqual(
					figure["completed"] + figure["loss"] + figure["pending"],
					ORDER_QTY, places=3,
					msg=f"{where}: completed {figure['completed']} + loss "
						f"{figure['loss']} + pending {figure['pending']} does not "
						f"account for {ORDER_QTY}",
				)
				self.assertGreaterEqual(figure["pending"], 0, f"{where}: pending")
				self.assertGreaterEqual(figure["loss"], 0, f"{where}: loss")

		return figures

	def pending(self, order):
		"""Pending per operation, with the law checked on the way past."""
		self.figures(order)
		return order.pending_by_operation()

	# ------------------------------------------------------------------
	# Loss downstream must not be charged to the operation before it
	# ------------------------------------------------------------------
	def test_each_operation_answers_for_itself(self):
		"""5 run at Embroidery for both items, 1 rejected at Folding, which only
		WO-A runs.

		Embroidery has run 5 of each item and destroyed none, so it owes 5 on each
		-- 10 in all -- and Folding's reject does not touch its row. Folding has
		run 5 of WO-A, 4 out and 1 destroyed, so it owes 5."""
		order = self.make_order({
			"Embroidery": {
				"WO-A": {"completed": 5.0, "loss": 0.0},
				"WO-B": {"completed": 5.0, "loss": 0.0},
			},
			"Folding": {
				"WO-A": {"completed": 4.0, "loss": 1.0},
			},
		})

		figures = self.figures(order)

		self.assertEqual(
			figures["Embroidery"]["WO-A"],
			{"completed": 5.0, "loss": 0.0, "pending": 5.0},
		)
		self.assertEqual(
			figures["Embroidery"]["WO-B"],
			{"completed": 5.0, "loss": 0.0, "pending": 5.0},
		)
		self.assertEqual(
			figures["Folding"]["WO-A"],
			{"completed": 4.0, "loss": 1.0, "pending": 5.0},
		)

	def test_cloth_destroyed_before_it_arrives_is_lost_to_this_operation(self):
		"""Folding reads 4 completed and 1 rejected in both orders below. What
		differs is Embroidery: it destroyed nothing in the first and half the order
		in the second, where it has also handled all 10 against Folding's 5.

		Where Embroidery has run the whole order, the cloth reaches it first and
		its 5 never arrive at Folding -- so for Folding they are lost, not
		outstanding, and its row reads 6 lost with nothing pending. Where nothing
		was destroyed anywhere, Folding still owes its 5."""
		untouched = self.make_order({
			"Embroidery": {
				"WO-A": {"completed": 5.0, "loss": 0.0},
				"WO-B": {"completed": 5.0, "loss": 0.0},
			},
			"Folding": {"WO-A": {"completed": 4.0, "loss": 1.0}},
		})
		half_destroyed = self.make_order({
			"Embroidery": {
				"WO-A": {"completed": 5.0, "loss": 5.0},
				"WO-B": {"completed": 5.0, "loss": 5.0},
			},
			"Folding": {"WO-A": {"completed": 4.0, "loss": 1.0}},
		})

		# Nothing destroyed anywhere: Folding's own reject only, and 5 still to run.
		self.assertEqual(
			self.figures(untouched)["Folding"]["WO-A"],
			{"completed": 4.0, "loss": 1.0, "pending": 5.0},
		)
		# Half destroyed ahead of it: 1 rejected here and 5 that never arrive.
		self.assertEqual(
			self.figures(half_destroyed)["Folding"]["WO-A"],
			{"completed": 4.0, "loss": 6.0, "pending": 0.0},
		)
		# Embroidery has run all 10 of each item -- 5 out, 5 destroyed -- so it is
		# through as well.
		self.assertEqual(self.pending(half_destroyed)["Embroidery"], {})

	def test_part_production_still_shows_as_pending(self):
		"""Pieces merely unfinished are still coming: 5 of 10 through each
		operation with nothing destroyed leaves 5 pending on both."""
		order = self.make_order({
			"Embroidery": {"WO-A": {"completed": 5.0, "loss": 0.0}},
			"Folding": {"WO-A": {"completed": 5.0, "loss": 0.0}},
		})

		self.assertEqual(self.pending(order), {
			"Folding": {"WO-A": 5.0},
			"Embroidery": {"WO-A": 5.0},
		})

	def test_an_operations_own_loss_is_work_it_has_done(self):
		"""Destroying a piece is running it: Embroidery put all 10 through, 7 out
		and 3 destroyed, so 7 and 3 account for its 10 and it owes nothing.

		Folding has handled only 5, so the cloth reaches it after Embroidery and
		Embroidery's 3 never arrive. Its row reads 5 completed, 3 lost, 2 still to
		run -- and 5, 3 and 2 account for the 10."""
		order = self.make_order({
			"Embroidery": {"WO-A": {"completed": 7.0, "loss": 3.0}},
			"Folding": {"WO-A": {"completed": 5.0, "loss": 0.0}},
		})

		figures = self.figures(order)
		self.assertEqual(
			figures["Embroidery"]["WO-A"],
			{"completed": 7.0, "loss": 3.0, "pending": 0.0},
		)
		self.assertEqual(
			figures["Folding"]["WO-A"],
			{"completed": 5.0, "loss": 3.0, "pending": 2.0},
		)

	def test_no_operation_order_is_read_anywhere(self):
		"""The same figures give the same answer whichever way round the operations
		table lists them, and whichever order the cards were filled in.

		Nothing holds the floor to a sequence, so nothing here assumes one: the row
		order of the operations table, which is the union over every item, must
		never change a quantity."""
		balances = {
			"Embroidery": {"WO-A": {"completed": 5.0, "loss": 0.0}},
			"Folding": {"WO-A": {"completed": 4.0, "loss": 1.0}},
		}

		listed_one_way = self.make_order(balances)

		listed_the_other = self.make_order(balances)
		listed_the_other.operations = list(reversed(listed_the_other.operations))

		self.assertEqual(
			listed_one_way.pending_by_operation(),
			listed_the_other.pending_by_operation(),
		)
		# Each row off its own figures: Embroidery has run 5 of 10 and owes 5;
		# Folding has run 5 (4 out, 1 destroyed) and owes 5.
		self.assertEqual(
			listed_one_way.pending_by_operation(),
			{"Embroidery": {"WO-A": 5.0}, "Folding": {"WO-A": 5.0}},
		)

	def test_everything_comes_off_the_master_work_orders_own_tables(self):
		"""No BOM, no Work Order Operation rows, no routing table is read.

		The order's items give the qty, the cards give each operation's completed
		and destroyed, and those are the whole of the input. This is the guard
		against a routing lookup creeping back in: the doc here has no Work Orders
		behind it at all, and the arithmetic still answers."""
		order = self.make_order({
			"Embroidery": {
				"WO-A": {"completed": 5.0, "loss": 0.0},
				"WO-B": {"completed": 5.0, "loss": 0.0},
			},
			"Folding": {"WO-A": {"completed": 4.0, "loss": 1.0}},
		})

		self.assertFalse(
			frappe.db.exists("Work Order", "WO-A"),
			"the fixture's Work Orders are made up -- nothing may look them up",
		)
		self.assertEqual(self.pending(order), {
			"Embroidery": {"WO-A": 5.0, "WO-B": 5.0},
			"Folding": {"WO-A": 5.0},
		})

	def test_finish_ceiling_is_the_least_any_operation_completed(self):
		"""A piece is only made once every operation that runs it has put it
		through, so WO-A is held to Folding's 4 and WO-B, which Folding never
		touches, to Embroidery's 5. No notion of a last operation is needed."""
		order = self.make_order({
			"Embroidery": {
				"WO-A": {"completed": 5.0, "loss": 5.0},
				"WO-B": {"completed": 5.0, "loss": 5.0},
			},
			"Folding": {
				"WO-A": {"completed": 4.0, "loss": 1.0},
			},
		})

		self.assertEqual(
			order.final_operation_output(),
			{"WO-A": 4.0, "WO-B": 5.0},
		)
