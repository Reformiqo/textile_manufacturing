# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document

# The Items column of an operation line.
#
# The same operation may be on the order more than once, once per Manufacturing
# Type -- Embroidery run in house on two of the colours and sent out on the third
# -- so the line has to say which of the order's items it is for. Held as a comma
# separated list of item codes rather than as a child table: the operation row is
# itself a child table and Frappe does not load a table inside one. The fieldname
# is item_codes rather than items so that reading it off a frappe._dict row picks
# up the value and not dict.items.
#
# Everything downstream reads the selection through here, so the split is stated
# in one place: split_items() to read a line, join_items() to write one.


def split_items(value):
	"""The item codes on an operation line, in the order they were picked.

	Duplicates are kept -- validate_operation_items() is what refuses them, and it
	cannot report what this has already quietly dropped."""
	if not value:
		return []

	return [item.strip() for item in str(value).replace("\n", ",").split(",") if item.strip()]


def join_items(items):
	"""The stored form of a selection, as the Items column carries it."""
	return ", ".join(items)


class MasterWorkOrderOperation(Document):
	def selected_items(self):
		return split_items(self.item_codes)
