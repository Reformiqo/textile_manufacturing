// Copyright (c) 2026, Reformiqo and contributors
// For license information, please see license.txt

frappe.ui.form.on("Master Work Order", {
    refresh: function(frm){
        if(frm.doc.docstatus != 1) return;

        frm.add_custom_button(__("Job Card"), () => {
        }, __("Create"));

        const has_master_job_card = (frm.doc.operations || []).some(
            (op) => op.master_job_card_number
        );
        if (!has_master_job_card) {
            frm.add_custom_button(__("Start Work Order"), () => {
                frm.call({
                    method: "start_job_card",
                    doc: frm.doc,
                    freeze: true,
                    freeze_message: __("Creating Master Job Cards..."),
                    callback: function () {
                        frm.reload_doc();
                    },
                });
            }, __("Create"));
        }

        frm.add_custom_button(__("Finish Work Order"), () => {
            frappe.confirm(__("Finish all linked Work Orders? This will produce the finished goods."), () => {
                frm.call({
                    method: "finish_work_orders",
                    doc: frm.doc,
                    freeze: true,
                    freeze_message: __("Finishing Work Orders..."),
                    callback: () => frm.reload_doc(),
                });
            });
        }, __("Create"));

        frm.add_custom_button(__("Close Work Order"), () => {
            frappe.confirm(__("Close all linked Work Orders?"), () => {
                frm.call({
                    method: "close_work_orders",
                    doc: frm.doc,
                    freeze: true,
                    freeze_message: __("Closing Work Orders..."),
                    callback: () => frm.reload_doc(),
                });
            });
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
    fg_warehouse: function (frm) {
        update_all_child_warehouses(frm);
    },
    wip_warehouse: function (frm) {
        update_all_child_warehouses(frm);
    },
    scrap_warehouse: function (frm) {
        update_all_child_warehouses(frm);
    },
});


function set_child_warehouses(frm, child) {
    child.source_warehouse = frm.doc.source_warehouse;
    child.fg_warehouse = frm.doc.fg_warehouse;
    child.wip_warehouse = frm.doc.wip_warehouse;
    child.scrap_warehouse = frm.doc.scrap_warehouse;
}

function update_all_child_warehouses(frm) {
    (frm.doc.items_to_be_manufacture || []).forEach((child) => {
        set_child_warehouses(frm, child);
    });
    (frm.doc.required_items || []).forEach((child) => {
        child.source_warehouse = frm.doc.source_warehouse
    });
    frm.refresh_field("items_to_be_manufacture");
    frm.refresh_field("required_items");
}