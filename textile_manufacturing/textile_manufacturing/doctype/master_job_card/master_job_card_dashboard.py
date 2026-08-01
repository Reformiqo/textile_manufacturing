from frappe import _


def get_data():
	return {
		"fieldname": "master_job_card",
		"internal_links": {
			"Job Card": ["job_card_detail", "job_card_number"],
		},
		"transactions": [
			{
				"label": _("Manufacturing"),
				"items": ["Job Card"],
			},
		],
	}
