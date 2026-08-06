// Copyright (c) 2026, Reformiqo and contributors
// For license information, please see license.txt

const NO_FURTHER_WORK = ["Completed", "Stopped", "Closed"];

frappe.ui.form.on("Master Work Order", {
    refresh: function(frm){
        if(frm.doc.docstatus != 1) return;

        add_status_buttons(frm);
        if (NO_FURTHER_WORK.includes(frm.doc.status)) return;

        add_create_buttons(frm);
        add_return_buttons(frm);
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
    add_finish_button(frm);
    add_pending_master_job_card_button(frm);

    add_subcontracted_po_button(frm);

    frm.page.set_inner_btn_group_as_primary(__("Create"));
}


function add_subcontracted_po_button(frm) {
    // Nothing goes out to a supplier unless an operation is routed Out House, so
    // there is no Purchase Order to raise.
    const out_house = (frm.doc.operations || []).some(
        (row) => row.manufacturing_type === "Out House"
    );
    if (!out_house) return;

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
    const rows = pending_transfer_rows(frm);
    if (!rows.length) return;

    frm.add_custom_button(__("Start"), () => {
        transfer_qty_dialog(frm, rows);
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
        primary_action_label: __("Next"),
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
                method: "get_transfer_materials",
                doc: frm.doc,
                args: { rows: selected },
                freeze: true,
                freeze_message: __("Working out the raw material..."),
            }).then((r) => {
                const materials = r.message || [];
                if (!materials.length) {
                    frappe.msgprint(__("There is no raw material to transfer for this qty."));
                    return;
                }
                transfer_materials_dialog(frm, selected, materials);
            });
        },
    });
    d.show();
}
 
 
function transfer_materials_dialog(frm, rows, materials) {
    const groups = [];
    const groups_by_wo = {};
 
    rows.forEach((r) => {
        const wo = r.work_order_number;
        if (!groups_by_wo[wo]) {
            groups_by_wo[wo] = { work_order_number: wo, item_code: r.item_code, materials: [] };
            groups.push(groups_by_wo[wo]);
        }
    });
 
    materials.forEach((m) => {
        const grp = groups_by_wo[m.work_order_number];
        if (grp) {
            grp.materials.push(m);
        }
    });
 
    const material_table_fields = [
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
            fieldname: "available_qty",
            label: __("Available"),
            in_list_view: 1,
            read_only: 1,
            columns: 1,
        },
        {
            fieldtype: "Float",
            fieldname: "suggested_qty",
            label: __("Required"),
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
            columns: 2,
        },
        {
            fieldtype: "Data",
            fieldname: "work_order_number",
            label: __("Work Order"),
            hidden: 1,
        },
        {
            fieldtype: "Int",
            fieldname: "row_id",
            label: __("Row"),
            hidden: 1,
        },
    ];
 
    // Build one Section Break + Table per item group so raw materials
    // render as separate tables instead of a single mixed list.
    const dialog_fields = [];
    groups.forEach((grp, idx) => {
        dialog_fields.push({
            fieldtype: "Section Break",
            label: `${grp.item_code} (${grp.materials.length} ${
                grp.materials.length === 1 ? "raw material" : "raw materials"
            })`,
        });
        dialog_fields.push({
            fieldtype: "Table",
            fieldname: `materials_${idx}`,
            cannot_add_rows: 1,
            cannot_delete_rows: 1,
            in_place_edit: false,
            data: grp.materials,
            get_data: () => grp.materials,
            fields: material_table_fields,
        });
    });
 
    const d = new frappe.ui.Dialog({
        title: __("Raw Material to Transfer"),
        size: "extra-large",
        fields: dialog_fields,
        primary_action_label: __("Start"),
        primary_action(values) {
            // Merge rows back from every per-item table into one flat array
            // before sending to the server -- the backend doesn't need to
            // know the dialog grouped them visually.
            let edited = [];
            groups.forEach((grp, idx) => {
                const table_values = values[`materials_${idx}`] || [];
                edited = edited.concat(table_values.filter((row) => flt(row.qty) > 0));
            });
 
            if (!edited.length) {
                frappe.msgprint(__("Enter a Qty to Transfer for at least one raw material."));
                return;
            }
 
            d.hide();
            frm.call({
                method: "start_material_transfer",
                doc: frm.doc,
                args: { rows: rows, materials: edited },
                freeze: true,
                freeze_message: __("Transferring material to WIP..."),
                callback: () => frm.reload_doc(),
            });
        },
    });
    d.show();
}

function add_finish_button(frm) {
    // From onload: what may be finished is capped by what the last operation turned
    // out, and that is not on the form.
    const rows = frm.doc.__onload?.pending_manufacture_rows || [];
    if (!rows.length) return;

    frm.add_custom_button(__("Finish"), () => {
        // The order still has goods to produce, but the line has not turned them out
        // yet. The button stays so the reason can be given -- hiding it leaves the
        // operator with nothing to click and nothing to read.
        const reason = frm.doc.__onload?.finish_blocked_reason;
        if (reason) {
            frappe.msgprint({
                title: __("Operations Not Complete"),
                message: reason,
                indicator: "orange",
            });
            return;
        }

        finish_qty_dialog(frm, rows.filter((row) => flt(row.qty) > 0).map((row) => ({ ...row })));
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
                        // ERPNext takes this off the entry itself, so Qty to Produce is
                        // the pieces put through: 10 with a loss of 5 books 5 as made.
                        fieldtype: "Float",
                        fieldname: "process_loss_qty",
                        label: __("Process Loss"),
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
                    __("{0}: only {1} can be produced -- that is what the last operation turned out and has not been booked yet.", [
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


function add_return_buttons(frm) {
    frm.call({
        method: "get_return_items",
        doc: frm.doc,
        freeze: true,
        freeze_message: __("Fetching returnable items..."),
    }).then((r) => {
        const groups = r.message || [];
        if (!groups.length) return;
        
        frm.add_custom_button(__("Return Component"), () => {
            show_return_dialog(frm, groups);
        }, __("Create"));
    });
}

function show_return_dialog(frm, groups) {
    const table_fields = [];

    groups.forEach((group, idx) => {
        table_fields.push({
            fieldtype: "Section Break",
            label: __(`BOM: ${group.bom_no}`),
        });
        table_fields.push({
            fieldtype: "Table",
            fieldname: `rows_${idx}`,
            cannot_add_rows: 1,
            cannot_delete_rows: 1,
            in_place_edit: false,
            data: group.items,
            get_data: () => group.items,
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
                    fieldtype: "Float",
                    fieldname: "transferred_qty",
                    label: __("Transferred"),
                    in_list_view: 1,
                    read_only: 1,
                    columns: 1,
                },
                {
                    fieldtype: "Float",
                    fieldname: "consumed_qty",
                    label: __("Consumed"),
                    in_list_view: 1,
                    read_only: 1,
                    columns: 1,
                },
                {
                    fieldtype: "Float",
                    fieldname: "returned_qty",
                    label: __("Already Returned"),
                    in_list_view: 1,
                    read_only: 1,
                    columns: 1,
                },
                {
                    fieldtype: "Float",
                    fieldname: "max_returnable",
                    label: __("Max Returnable"),
                    in_list_view: 1,
                    read_only: 1,
                    columns: 1,
                    hidden: 1
                },
                {
                    fieldtype: "Float",
                    fieldname: "qty",
                    label: __("Qty to Return"),
                    in_list_view: 1,
                    reqd: 1,
                    columns: 1,
                },
                {
                    fieldtype: "Data",
                    fieldname: "work_order_number",
                    label: __("Work Order"),
                    hidden: 1,
                },
            ],
        });
    });

    const d = new frappe.ui.Dialog({
        title: __("Return Components"),
        size: "extra-large",
        fields: table_fields,
        primary_action_label: __("Return"),
        primary_action(values) {
            const all_rows = groups.map((_, idx) => values[`rows_${idx}`] || []).flat();
            const selected = all_rows.filter((row) => flt(row.qty) > 0);

            if (!selected.length) {
                frappe.msgprint(__("Enter a Qty to Return for at least one item."));
                return;
            }

            const negative = selected.find((row) => flt(row.qty) < 0);
            if (negative) {
                frappe.msgprint(__("Qty to Return cannot be negative."));
                return;
            }

            const over = selected.find((row) => flt(row.qty) > flt(row.max_returnable));
            if (over) {
                frappe.msgprint(
                    __("{0} ({1}): Qty to Return must not be more than the returnable {2}.", [
                        over.item_code,
                        over.work_order_number,
                        format_number(over.max_returnable),
                    ]),
                );
                return;
            }

            d.hide();
            frm.call({
                method: "create_return_stock_entry",
                doc: frm.doc,
                args: { items: selected },
                freeze: true,
                freeze_message: __("Creating Stock Return Entries..."),
            }).then((r) => {
                const names = r.message || [];
                if (names.length) {
                    frappe.msgprint(__("Stock Return Entries created: {0}", [names.join(", ")]));
                    frm.reload_doc();
                }
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
    // Whether anything is left to run is settled in onload, off the Master Job Cards
    // -- the same way the Work Order settles its own Create Job Card button.
    if (!frm.doc.__onload?.show_pending_master_job_card_button) return;
    if (!(frm.doc.operations || []).length) return;

    frm.add_custom_button(__("Pending Master Job Card"), () => {
        pending_master_job_card_dialog(frm);
    }, __("Create"));
}


function pending_master_job_card_dialog(frm) {
    const operations_data = [];

    const dialog = frappe.prompt(
        {
            fieldname: "operations",
            fieldtype: "Table",
            label: __("Operations"),
            fields: [
                {
                    fieldtype: "Link",
                    fieldname: "opration_name",
                    label: __("Operation"),
                    options: "Operation",
                    read_only: 1,
                    in_list_view: 1,
                },
                {
                    fieldtype: "Link",
                    fieldname: "workstation",
                    label: __("Workstation"),
                    options: "Workstation",
                    read_only: 1,
                    in_list_view: 1,
                },
                {
                    fieldtype: "Float",
                    fieldname: "qty",
                    label: __("Pending Qty"),
                    read_only: 1,
                    in_list_view: 1,
                },
                {
                    fieldtype: "Int",
                    fieldname: "opration_sequence_no",
                    label: __("Sequence Id"),
                    read_only: 1,
                },
            ],
            data: operations_data,
            in_place_edit: true,
            get_data: () => operations_data,
        },
        function () {
            const selected_rows = dialog.fields_dict["operations"].grid.get_selected_children();
            if (!selected_rows.length) {
                frappe.msgprint(
                    __("Please select atleast one operation to create a Master Job Card")
                );
                return;
            }

            frm.call({
                method: "make_pending_master_job_cards",
                doc: frm.doc,
                args: { operations: selected_rows },
                freeze: true,
                freeze_message: __("Raising Master Job Cards for the pending qty..."),
            }).then(() => frm.reload_doc());
        },
        __("Pending Master Job Card"),
        __("Create")
    );

    dialog.fields_dict["operations"].grid.grid_buttons.hide();

    // What the order asked of each operation, less everything its Master Job Cards
    // accounted for -- made, lost or rejected. Off onload alone, computed fresh
    // every time the form opens: the operation rows carry a Pending Qty of their
    // own, but it is only rewritten when a card syncs, and an operation the server
    // no longer counts as pending must not come back on that stale figure.
    const pending_qty = {};
    (frm.doc.__onload?.pending_master_job_card_operations || []).forEach((row) => {
        pending_qty[row.opration_name] = flt(row.qty);
    });

    (frm.doc.operations || []).forEach((row) => {
        if (row.manufacturing_type !== "In-House") return;

        const qty = flt(pending_qty[row.opration_name]);
        if (qty <= 0) return;

        dialog.fields_dict.operations.df.data.push({
            __checked: 1,
            opration_name: row.opration_name,
            workstation: row.workstation,
            opration_sequence_no: row.opration_sequence_no,
            qty: qty,
        });
    });

    dialog.fields_dict.operations.grid.refresh();
}
