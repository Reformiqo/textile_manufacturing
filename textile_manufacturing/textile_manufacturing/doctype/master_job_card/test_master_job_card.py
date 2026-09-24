# Copyright (c) 2026, Reformiqo and Contributors
# See license.txt

"""What a Master Job Card may run when a supplier stands before it.

An operation fed by an Out House line has no previous Master Job Card to read --
there is no card, there is a Purchase Order -- so what it may work is what has
actually come back. 5 back off an order for 100 is 5.

UnitTestCase rather than IntegrationTestCase, for the reason the Master Work
Order's own tests give: the integration base creates and commits test records for
every link dependency, which on a site with real data leaves rows behind for good.
Nothing here touches the database -- what the supplier returned is handed straight
in, and the reckoning is the whole of the question.
"""

import frappe
from frappe.tests import UnitTestCase
from frappe.utils import flt

ORDER_QTY = 100.0
RECEIVED = 5.0


class TestOutHouseFeedCeiling(UnitTestCase):
	RED, BLUE = "ABC - Red", "ABC - Blue"

	def make_card(self, feed=None, rows=None):
		"""A Cut Work card for two colours, with what its supplier sent back."""
		card = frappe.new_doc("Master Job Card")
		card.master_work_order_number = "MWO-0001"
		card.operation_name = "CUT WORK"

		for item_code, work_order in ((self.RED, "WO-RED"), (self.BLUE, "WO-BLUE")):
			card.append("job_card_detail", {
				"item_code": item_code,
				"item_name": item_code,
				"work_order_number": work_order,
				"qty_to_manufacture": ORDER_QTY,
				"completed_qty": 0.0,
			})

		card.out_house_feed_ceiling = lambda: dict(feed or {})
		card.own_operation_loss = lambda: {}
		card.master_work_order_items = lambda: [
			frappe._dict(row) for row in (rows or [
				{"work_order_number": "WO-RED", "qty_to_manufacture": ORDER_QTY,
				 "process_loss_qty": 0.0, "manufacture_qty": 0.0},
				{"work_order_number": "WO-BLUE", "qty_to_manufacture": ORDER_QTY,
				 "process_loss_qty": 0.0, "manufacture_qty": 0.0},
			])
		]

		return card

	# ------------------------------------------------------------------
	# The In Stock button's rows
	# ------------------------------------------------------------------
	def test_in_stock_offers_what_came_back_and_not_the_order(self):
		"""5 received off an order for 100 offers 5. 5 is all the floor has."""
		card = self.make_card(feed={"WO-RED": RECEIVED, "WO-BLUE": RECEIVED})

		self.assertEqual(
			[(row["item_code"], row["qty"]) for row in card.sfg_item_rows()],
			[(self.RED, RECEIVED), (self.BLUE, RECEIVED)],
		)

	def test_a_row_with_nothing_back_is_not_offered_at_all(self):
		"""And a card left with no row offers no button -- there is nothing to
		put into store or take out of it."""
		card = self.make_card(feed={"WO-RED": 0.0, "WO-BLUE": 0.0})

		self.assertEqual(card.sfg_item_rows(), [])

	def test_a_work_order_no_supplier_feeds_is_offered_what_it_always_was(self):
		"""The reds went out and came back 5; the blues never left the floor."""
		card = self.make_card(feed={"WO-RED": RECEIVED})

		self.assertEqual(
			[(row["item_code"], row["qty"]) for row in card.sfg_item_rows()],
			[(self.RED, RECEIVED), (self.BLUE, ORDER_QTY)],
		)

	def test_no_supplier_at_all_leaves_the_rows_untouched(self):
		card = self.make_card()

		self.assertEqual(
			[row["qty"] for row in card.sfg_item_rows()],
			[ORDER_QTY, ORDER_QTY],
		)

	# ------------------------------------------------------------------
	# What may be completed
	# ------------------------------------------------------------------
	def test_the_cap_is_held_to_what_came_back(self):
		card = self.make_card(feed={"WO-RED": RECEIVED, "WO-BLUE": RECEIVED})

		self.assertEqual(
			card.qty_caps(), {"WO-RED": RECEIVED, "WO-BLUE": RECEIVED},
		)

	def test_the_cap_is_the_lower_of_the_two_reckonings(self):
		"""The supplier sent back 5, but 98 of the order are already gone -- 2
		lost and 96 finished. The card may run 2, not 5."""
		card = self.make_card(
			feed={"WO-RED": RECEIVED},
			rows=[{
				"work_order_number": "WO-RED",
				"qty_to_manufacture": ORDER_QTY,
				"process_loss_qty": 2.0,
				"manufacture_qty": 96.0,
			}],
		)

		self.assertEqual(card.qty_caps(), {"WO-RED": 2.0})

	def test_a_work_order_no_supplier_feeds_is_capped_as_it_always_was(self):
		card = self.make_card(feed={"WO-RED": RECEIVED})

		self.assertEqual(
			flt(card.qty_caps()["WO-BLUE"]), ORDER_QTY,
			"no supplier stands between WO-BLUE and its cloth",
		)

	# ------------------------------------------------------------------
	# Starting the card
	# ------------------------------------------------------------------
	def test_the_card_cannot_start_before_anything_comes_back(self):
		card = self.make_card(feed={"WO-RED": 0.0, "WO-BLUE": 0.0})

		with self.assertRaises(frappe.ValidationError) as refused:
			card.validate_out_house_material_received()
		self.assertIn("CUT WORK", str(refused.exception))

	def test_part_of_it_back_is_work(self):
		"""5 back off 100 is 5 to run, and the caps hold it to the 5."""
		card = self.make_card(feed={"WO-RED": RECEIVED, "WO-BLUE": 0.0})

		card.validate_out_house_material_received()

	def test_a_card_no_supplier_feeds_starts_as_it_always_did(self):
		card = self.make_card()

		card.validate_out_house_material_received()
