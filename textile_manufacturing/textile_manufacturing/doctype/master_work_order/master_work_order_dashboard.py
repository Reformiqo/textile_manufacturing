def get_data():
	return {
		"fieldname": "master_work_order_number",
		"non_standard_fieldnames": {
			"Purchase Order": "master_work_order",
		},
		"transactions": [
			{
				"label": ("Manufacturing"),
				"items": ["Master Job Card", "Purchase Order"],
			}
		],
	}