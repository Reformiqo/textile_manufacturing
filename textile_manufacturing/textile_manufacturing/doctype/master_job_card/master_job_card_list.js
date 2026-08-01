frappe.listview_settings["Master Job Card"] = {
    add_fields: ["status"],

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
