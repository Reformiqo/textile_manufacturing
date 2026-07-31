// Copyright (c) 2026, Reformiqo and contributors
// For license information, please see license.txt

frappe.ui.form.on("Master Job Card", {
    refresh: function (frm) {
        if (frm.doc.master_work_order_number) {
            load_inhouse_operations(frm);
        }
        toggle_material_tab(frm);
    },

    master_work_order_number: function (frm) {
        if (!frm.doc.master_work_order_number) {
            frm._inhouse_operations = null
            return;
        }
        load_inhouse_operations(frm);
    },

    material_transfer_on: function (frm) {
        toggle_material_tab(frm);
    },

    operation_name: function (frm) {
        if (!frm.doc.master_work_order_number || !frm.doc.operation_name) {
            return;
        }
        fetch_from_master_work_order(frm);
    },
});


function toggle_material_tab(frm) {
    const read_only = frm.doc.material_transfer_on === "Work Oder";
    frm.set_df_property("required_item", "read_only", read_only ? 1 : 0);
    frm.refresh_field("required_item");
}


function load_inhouse_operations(frm) {
    frappe.call({
        method: "textile_manufacturing.textile_manufacturing.doctype.master_job_card.master_job_card.get_inhouse_operations",
        args: { master_work_order: frm.doc.master_work_order_number },
        callback: function (r) {
            frm._inhouse_operations = r.message || [];
        },
    });
}


function fetch_from_master_work_order(frm) {
    frm.call({
        method: "fetch_from_master_work_order",
        doc: frm.doc,
        freeze: true,
        freeze_message: __("Fetching details from Master Work Order..."),
        callback: function () {
            frm.refresh_fields();
            frappe.show_alert({
                message: __("Details fetched from Master Work Order"),
                indicator: "green",
            });
        },
    });
}
