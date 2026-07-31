from frappe import _


def get_data():
	return {
		"fieldname": "master_work_order_number",
		"internal_links": {
			"Work Order": ["items_to_be_manufacture", "work_order_number"],
		},
		"transactions": [
			{
				"label": _("Manufacturing"),
				"items": ["Work Order", "Master Job Card"],
			},
		],
	}
