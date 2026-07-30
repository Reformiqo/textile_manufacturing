from erpnext.manufacturing.doctype.production_plan.production_plan_dashboard import (
    get_data as erpnext_get_data,
)

def get_data(data=None):
    data = erpnext_get_data()

    # Add Master Work Order to Transactions
    for section in data.get("transactions", []):
        if section.get("label") == "Transactions":
            if "Master Work Order" not in section["items"]:
                section["items"].append("Master Work Order")

    # Tell Frappe how Master Work Order is linked to Production Plan
    data["non_standard_fieldnames"]["Master Work Order"] = "production_plan_number"

    return data