frappe.listview_settings["Master Job Card"] = {
    add_fields: ["status"],

    // The card is worked while it is still a draft -- it is submitted only once the
    // operation finishes. Without this flag frappe.get_indicator() short-circuits any
    // submittable doctype at docstatus 0 straight to "Draft" and never reaches the
    // function below, so Open / Work In Progress / On Hold would never be seen.
    has_indicator_for_draft: 1,

    // Show the custom Status field as the list indicator instead of the
    // Draft/Submitted docstatus.
    get_indicator: function (doc) {
        const colors = {
            "Draft": "red",
            "Open": "orange",
            "Material Transferred": "blue",
            "Work In Progress": "yellow",
            "On Hold": "red",
            "Completed": "green",
            "Cancelled": "gray",
        };
        return [__(doc.status), colors[doc.status] || "gray", "status,=," + doc.status];
    },
};
