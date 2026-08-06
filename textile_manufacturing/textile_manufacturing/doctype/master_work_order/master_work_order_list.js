frappe.listview_settings["Master Work Order"] = {
    add_fields: ["status"],

    get_indicator: function (doc) {
        const colors = {
            "Draft": "red",
            "Not Started": "red",
            "In Process": "orange",
            "Completed": "green",
            "Stopped": "red",
            "Closed": "grey",
            "Cancelled": "gray",
        };
        return [__(doc.status), colors[doc.status] || "gray", "status,=," + doc.status];
    },
};
