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

        create_master_word_order(frm)
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


function fetch_production_plan_items(frm){
    frappe.call({
        method: "frappe.client.get",
        args: {
            doctype: "Production Plan",
            name: frm.doc.production_plan_number,
        },
        callback: function (r) {
            if (!r.message) return;

            const plan = r.message;
            frm.clear_table("items_to_be_manufacture");

            (plan.po_items || []).forEach((item) => {
                const child = frm.add_child("items_to_be_manufacture");

                // From Production Plan
                child.item_code = item.item_code;
                child.production_plan_number = frm.doc.production_plan_number;
                child.qty_to_manufacture = item.planned_qty;
                child.bom_no = item.bom_no

                // Auto-fetch from parent warehouse fields
                set_child_warehouses(frm, child);
            });

            frm.refresh_field("items_to_be_manufacture");
            frappe.show_alert({
                message: __("Items fetched from Production Plan"),
                indicator: "green",
            });
        },
    });
}

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