// Copyright (c) 2026, Reformiqo and contributors
// For license information, please see license.txt

const NO_FURTHER_WORK = ["Completed", "Stopped", "Closed"];

frappe.ui.form.on("Master Work Order", {
    refresh: function(frm){
        if(frm.doc.docstatus != 1) return;

        add_status_buttons(frm);
        if (NO_FURTHER_WORK.includes(frm.doc.status)) return;

        add_create_buttons(frm);
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


function add_create_buttons(frm) {
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
                callback: () => frm.reload_doc(),
            });
        }, __("Create"));
    }

    add_pending_master_job_card_button(frm);

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

    frm.add_custom_button(__("Create Subcontracted PO"), () => {
        frm.call({
            method: "make_subcontracted_purchase_order",
            doc: frm.doc,
            freeze: true,
            freeze_message: __("Preparing Purchase Order..."),
            callback: function (r) {
                if (!r.message) return;
                const purchase_order = frappe.model.sync(r.message)[0];
                frappe.set_route("Form", purchase_order.doctype, purchase_order.name);
            },
        });
    }, __("Create"));
}


function add_status_buttons(frm) {
    if (["Completed", "Cancelled"].includes(frm.doc.status)) return;

    if (frm.doc.status === "Stopped") {
        frm.add_custom_button(__("Re-Open"), () => {
            change_status(frm, "reopen_work_orders", __("Re-opening Work Orders..."));
        }, __("Status"));
        return;
    }

    if (frm.doc.status === "Closed") return;

    frm.add_custom_button(__("Close"), () => {
        frappe.confirm(
            __("Once the Master Work Order is Closed it can't be resumed"),
            () => change_status(frm, "close_work_orders", __("Closing Work Orders...")),
        );
    }, __("Status"));

    frm.add_custom_button(__("Stop"), () => {
        frappe.confirm(
            __("Stop this Master Work Order ?"),
            () => change_status(frm, "stop_work_orders", __("Stopping Work Orders...")),
        );
    }, __("Status"));
}


function change_status(frm, method, freeze_message) {
    frm.call({
        method: method,
        doc: frm.doc,
        freeze: true,
        freeze_message: freeze_message,
        callback: () => frm.reload_doc(),
    });
}


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

// Create > Pending Master Job Card -- part production. Raises a fresh card for
// the balance of an operation once an earlier card has been completed with only
// part of the qty. The Pending Qty column of the operations table is the base.
function add_pending_master_job_card_button(frm) {
    const pending_operations = (frm.doc.operations || []).filter(
        (op) => op.manufacturing_type === "In-House" && flt(op.pending_qty) > 0
    );
    if (!pending_operations.length) return;

    frm.add_custom_button(__("Pending Master Job Card"), () => {
        pending_master_job_card_dialog(frm, pending_operations);
    }, __("Create"));
}


function pending_master_job_card_dialog(frm, pending_operations) {
    const d = new frappe.ui.Dialog({
        title: __("Create Master Job Card for Pending Qty"),
        fields: [
            {
                fieldtype: "HTML",
                fieldname: "help",
                options: `<p class="text-muted small">${__(
                    "Leave the selection empty to cover every operation that still has pending qty."
                )}</p>`,
            },
            {
                fieldtype: "MultiSelectPills",
                fieldname: "operations",
                label: __("Operations"),
                get_data: () =>
                    pending_operations.map((op) => ({
                        value: op.opration_name,
                        description: __("Pending {0}", [format_number(op.pending_qty)]),
                    })),
            },
        ],
        primary_action_label: __("Create"),
        primary_action(values) {
            d.hide();
            frm.call({
                method: "make_pending_master_job_cards",
                doc: frm.doc,
                args: { operations: values.operations || [] },
                freeze: true,
                freeze_message: __("Creating Master Job Card for the pending qty..."),
            }).then(() => frm.reload_doc());
        },
    });
    d.show();
}
