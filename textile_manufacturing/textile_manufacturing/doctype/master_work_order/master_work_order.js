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
    add_start_button(frm);

    add_pending_master_job_card_button(frm);

    add_finish_button(frm);

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

    frm.page.set_inner_btn_group_as_primary(__("Create"));
}


function pending_transfer_rows(frm) {
    return (frm.doc.items_to_be_manufacture || [])
        .filter((row) => row.work_order_number)
        .map((row) => ({
            work_order_number: row.work_order_number,
            item_code: row.item_code,
            s_warehouse: row.source_warehouse || frm.doc.source_warehouse,
            t_warehouse: row.wip_warehouse || frm.doc.wip_warehouse,
            qty_to_manufacture: flt(row.qty_to_manufacture),
            transferred_qty: flt(row.mateial_transfer_qty),
            pending_qty: flt(row.qty_to_manufacture) - flt(row.mateial_transfer_qty),
        }))
        .filter((row) => row.pending_qty > 0)
        .map((row) => ({ ...row, qty: row.pending_qty }));
}


function add_start_button(frm) {
    if (frm.doc.skip_material_transfer_to_wip_warehouse || frm.doc.material_transfer_on === "Job Card") {
        return;
    }
    if (!pending_transfer_rows(frm).length) return;

    frm.add_custom_button(__("Start"), () => {
        transfer_qty_dialog(frm, pending_transfer_rows(frm));
    }, __("Create"));
}


function transfer_qty_dialog(frm, rows) {
    const d = new frappe.ui.Dialog({
        title: __("Material Transfer to WIP Warehouse"),
        size: "extra-large",
        fields: [
            {
                fieldtype: "Table",
                fieldname: "rows",
                cannot_add_rows: 1,
                cannot_delete_rows: 1,
                in_place_edit: false,
                data: rows,
                get_data: () => rows,
                fields: [
                    {
                        fieldtype: "Link",
                        fieldname: "item_code",
                        label: __("Item"),
                        options: "Item",
                        in_list_view: 1,
                        read_only: 1,
                        columns: 2,
                    },
                    {
                        fieldtype: "Link",
                        fieldname: "s_warehouse",
                        label: __("Source Warehouse"),
                        options: "Warehouse",
                        in_list_view: 1,
                        read_only: 1,
                        columns: 2,
                    },
                    {
                        fieldtype: "Link",
                        fieldname: "t_warehouse",
                        label: __("WIP Warehouse"),
                        options: "Warehouse",
                        in_list_view: 1,
                        read_only: 1,
                        columns: 2,
                    },
                    {
                        fieldtype: "Float",
                        fieldname: "qty_to_manufacture",
                        label: __("Qty to Manufacture"),
                        in_list_view: 1,
                        read_only: 1,
                        columns: 1,
                    },
                    {
                        fieldtype: "Float",
                        fieldname: "transferred_qty",
                        label: __("Transferred"),
                        in_list_view: 1,
                        read_only: 1,
                        columns: 1,
                    },
                    {
                        fieldtype: "Float",
                        fieldname: "pending_qty",
                        label: __("Pending"),
                        in_list_view: 1,
                        read_only: 1,
                        columns: 1,
                    },
                    {
                        fieldtype: "Float",
                        fieldname: "qty",
                        label: __("Qty to Transfer"),
                        in_list_view: 1,
                        reqd: 1,
                        columns: 1,
                    },
                    {
                        // Carried so the server knows which order each qty belongs
                        // to; never shown -- it is plumbing, not information.
                        fieldtype: "Data",
                        fieldname: "work_order_number",
                        label: __("Work Order"),
                        hidden: 1,
                    },
                ],
            },
        ],
        primary_action_label: __("Start"),
        primary_action(values) {
            const selected = (values.rows || []).filter((row) => flt(row.qty) > 0);
            if (!selected.length) {
                frappe.msgprint(__("Enter a Qty to Transfer for at least one item."));
                return;
            }

            const over = selected.find((row) => flt(row.qty) > flt(row.pending_qty));
            if (over) {
                frappe.msgprint(
                    __("{0}: Qty to Transfer must not be more than the pending {1}.", [
                        over.item_code,
                        format_number(over.pending_qty),
                    ]),
                );
                return;
            }

            d.hide();
            frm.call({
                method: "start_material_transfer",
                doc: frm.doc,
                args: { rows: selected },
                freeze: true,
                freeze_message: __("Transferring material to WIP..."),
                callback: () => frm.reload_doc(),
            });
        },
    });
    d.show();
}


function pending_manufacture_rows(frm) {
    const skip = frm.doc.skip_material_transfer_to_wip_warehouse;

    return (frm.doc.items_to_be_manufacture || [])
        .filter((row) => row.work_order_number)
        .map((row) => {
            const ceiling = skip ? flt(row.qty_to_manufacture) : flt(row.mateial_transfer_qty);
            return {
                work_order_number: row.work_order_number,
                item_code: row.item_code,
                t_warehouse: row.fg_warehouse || frm.doc.fg_warehouse,
                qty_to_manufacture: flt(row.qty_to_manufacture),
                transferred_qty: flt(row.mateial_transfer_qty),
                produced_qty: flt(row.manufacture_qty),
                pending_qty: ceiling - flt(row.manufacture_qty),
            };
        })
        .filter((row) => row.pending_qty > 0)
        .map((row) => ({ ...row, qty: row.pending_qty }));
}


function add_finish_button(frm) {
    if (!pending_manufacture_rows(frm).length) return;

    frm.add_custom_button(__("Finish"), () => {
        finish_qty_dialog(frm, pending_manufacture_rows(frm));
    }, __("Create"));
}


function finish_qty_dialog(frm, rows) {
    const d = new frappe.ui.Dialog({
        title: __("Finish -- Produce Finished Goods"),
        size: "extra-large",
        fields: [
            {
                fieldtype: "Table",
                fieldname: "rows",
                cannot_add_rows: 1,
                cannot_delete_rows: 1,
                in_place_edit: false,
                data: rows,
                get_data: () => rows,
                fields: [
                    {
                        fieldtype: "Link",
                        fieldname: "item_code",
                        label: __("Item"),
                        options: "Item",
                        in_list_view: 1,
                        read_only: 1,
                        columns: 3,
                    },
                    {
                        fieldtype: "Link",
                        fieldname: "t_warehouse",
                        label: __("Target Warehouse"),
                        options: "Warehouse",
                        in_list_view: 1,
                        read_only: 1,
                        columns: 2,
                    },
                    {
                        fieldtype: "Float",
                        fieldname: "qty_to_manufacture",
                        label: __("Qty to Manufacture"),
                        in_list_view: 1,
                        read_only: 1,
                        columns: 1,
                    },
                    {
                        fieldtype: "Float",
                        fieldname: "transferred_qty",
                        label: __("Transferred"),
                        in_list_view: 1,
                        read_only: 1,
                        columns: 1,
                    },
                    {
                        fieldtype: "Float",
                        fieldname: "produced_qty",
                        label: __("Produced"),
                        in_list_view: 1,
                        read_only: 1,
                        columns: 1,
                    },
                    {
                        fieldtype: "Float",
                        fieldname: "pending_qty",
                        label: __("Pending"),
                        in_list_view: 1,
                        read_only: 1,
                        columns: 1,
                    },
                    {
                        fieldtype: "Float",
                        fieldname: "qty",
                        label: __("Qty to Produce"),
                        in_list_view: 1,
                        reqd: 1,
                        columns: 2,
                    },
                    {
                        fieldtype: "Data",
                        fieldname: "work_order_number",
                        label: __("Work Order"),
                        hidden: 1,
                    },
                ],
            },
        ],
        primary_action_label: __("Finish"),
        primary_action(values) {
            const selected = (values.rows || []).filter((row) => flt(row.qty) > 0);
            if (!selected.length) {
                frappe.msgprint(__("Enter a Qty to Produce for at least one item."));
                return;
            }

            const over = selected.find((row) => flt(row.qty) > flt(row.pending_qty));
            if (over) {
                frappe.msgprint(
                    __("{0}: Qty to Produce must not be more than the pending {1}.", [
                        over.item_code,
                        format_number(over.pending_qty),
                    ]),
                );
                return;
            }

            d.hide();
            frm.call({
                method: "finish_work_orders",
                doc: frm.doc,
                args: { rows: selected },
                freeze: true,
                freeze_message: __("Producing finished goods..."),
                callback: () => frm.reload_doc(),
            });
        },
    });
    d.show();
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


function add_pending_master_job_card_button(frm) {
    // The server decides: every Master Job Card completed, and the order still short.
    frm.call("pending_master_job_card_operations").then((r) => {
        const pending_operations = r.message || [];
        if (!pending_operations.length) return;

        frm.add_custom_button(__("Pending Master Job Card"), () => {
            pending_master_job_card_dialog(frm, pending_operations);
        }, __("Create"));
    });
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
