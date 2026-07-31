// Copyright (c) 2026, Reformiqo and contributors
// For license information, please see license.txt

frappe.ui.form.on("Master Work Order", {
    refresh: function(frm){
        frm.add_custom_button(__("Job Card"), () => {
        }, __("Create"));

        frm.add_custom_button(__("Start Work Order"), () => {
        }, __("Create"));

        frm.add_custom_button(__("Finish Work Order"), () => {
        }, __("Create"));

        frm.add_custom_button(__("Close Work Order"), () => {
        }, __("Create"));
    },

    production_plan_number: function (frm) {
        if (!frm.doc.production_plan_number) {
            return;
        }

        frm.call({
            method: "fetch_from_production_plan",
            doc: frm.doc,
            freeze: true,
            freeze_message: __("Fetching details from Production Plan..."),
            callback: function () {
                frm.refresh_fields();
                frappe.show_alert({
                    message: __("Details fetched from Production Plan"),
                    indicator: "green",
                });
            },
        });
    },

    // Whenever parent warehouse fields change, push the update to all existing rows
    source_warehouse: function (frm) {
        update_all_child_warehouses(frm);
    },
    target_warehouse: function (frm) {
        update_all_child_warehouses(frm);
    },
    work_in_progress_warehouse: function (frm) {
        update_all_child_warehouses(frm);
    },
    scrape_warhouse: function (frm) {
        update_all_child_warehouses(frm);
    },
});


function set_child_warehouses(frm, child) {
    child.source_warehouse = frm.doc.source_warehouse;
    child.target_warehouse = frm.doc.target_warehouse;
    child.work_in_progress_warehouse = frm.doc.work_in_progress_warehouse;
    child.scrape_warhouse = frm.doc.scrape_warhouse;
}

function update_all_child_warehouses(frm) {
    (frm.doc.items_to_be_manufacture || []).forEach((child) => {
        set_child_warehouses(frm, child);
    });
    frm.refresh_field("items_to_be_manufacture");
}