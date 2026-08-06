import frappe


def update_master_job_card_detail(doc, method=None):
    if doc.reference_type != "Job Card" or not doc.reference_name:
        return

    clearing = method == "on_trash" or doc.docstatus == 2

    filters = {
        "parenttype": "Master Job Card",
        "job_card_number": doc.reference_name,
        "item_code": doc.item_code,
    }
    if clearing:
        filters["quality_inspection"] = doc.name

    for row in frappe.get_all("Master Job Card Detail", filters=filters, pluck="name"):
        frappe.db.set_value(
            "Master Job Card Detail",
            row,
            "quality_inspection",
            "" if clearing else doc.name,
            update_modified=False,
        )
