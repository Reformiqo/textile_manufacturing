frappe.listview_settings["Master Work Order"] = {
    add_fields: ["status"],

    // Show the custom Status field as the list indicator instead of the
    // Draft/Submitted docstatus, the same way a Work Order reads.
    get_indicator: function (doc) {
        const colors = {
            "Draft": "red",
            "Not Started": "orange",
            "In Process": "blue",
            "Completed": "green",
            "Stopped": "red",
            "Cancelled": "gray",
        };
        return [__(doc.status), colors[doc.status] || "gray", "status,=," + doc.status];
    },
};
