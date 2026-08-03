// Copyright (c) 2026, Reformiqo and contributors
// For license information, please see license.txt

frappe.ui.form.on("Master Job Card", {
    refresh: function (frm) {
        if (frm.doc.master_work_order_number) {
            load_inhouse_operations(frm);
        }
        toggle_material_tab(frm);
        add_action_buttons(frm);
        add_quality_inspection_button(frm);
        render_job_timer(frm);
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


frappe.ui.form.on("Master Job Card Time Log", {
    from_time: function (frm, cdt, cdn) {
        set_time_in_mins(cdt, cdn);
    },
    to_time: function (frm, cdt, cdn) {
        set_time_in_mins(cdt, cdn);
    },
});


function set_time_in_mins(cdt, cdn) {
    const row = locals[cdt][cdn];
    if (row.from_time && row.to_time) {
        const mins = moment(row.to_time).diff(moment(row.from_time), "seconds") / 60;
        frappe.model.set_value(cdt, cdn, "time_in_mins", mins);
    } else {
        frappe.model.set_value(cdt, cdn, "time_in_mins", 0);
    }
}


function add_action_buttons(frm) {
    if (frm.is_new() || frm.doc.docstatus !== 0 || frm.doc.material_transfer_on !== "Job Card") return;

    frm.add_custom_button(__("Material Transfer for Manufacture"), () => {
        frm.call({
            method: "make_material_transfer_for_manufacture",
            doc: frm.doc,
            freeze: true,
            freeze_message: __("Building material transfer entry..."),
        }).then((r) => {
            if (r.message) {
                const doc = frappe.model.sync(r.message)[0];
                frappe.set_route("Form", doc.doctype, doc.name);
            }
        });
    }, __("Create"));
}


function add_quality_inspection_button(frm) {
    // Inspection happens while the operation is being worked, so on the draft card.
    if (!frm.doc.quality_inspection_requied || frm.is_new() || frm.doc.docstatus !== 0) return;

    const pending = (frm.doc.job_card_detail || []).filter(
        (row) => row.job_card_number && !row.quality_inspection
    );
    if (!pending.length) return;

    frm.add_custom_button(__("Quality Inspection"), () => {
        quality_inspection_dialog(frm, pending);
    }, __("Create"));
}


function quality_inspection_dialog(frm, pending) {
    const d = new frappe.ui.Dialog({
        title: __("Quality Inspection"),
        fields: [
            {
                fieldtype: "Select",
                fieldname: "detail_row",
                label: __("Item"),
                reqd: 1,
                default: pending[0].name,
                options: pending.map((row) => ({
                    label: `${row.item_code}`,
                    value: row.name,
                })),
            },
        ],
        primary_action_label: __("Create"),
        primary_action(values) {
            d.hide();
            frm.call({
                method: "make_quality_inspection",
                doc: frm.doc,
                args: { detail_row: values.detail_row },
                freeze: true,
                freeze_message: __("Building Quality Inspection..."),
            }).then((r) => {
                if (r.message) {
                    const doc = frappe.model.sync(r.message)[0];
                    frappe.set_route("Form", doc.doctype, doc.name);
                }
            });
        },
    });
    d.show();
}


function render_job_timer(frm){
    if (frm.is_new() || frm.doc.docstatus !== 0) return;
    if ((frm.doc.job_card_detail || []).every((r) => !r.job_card_number)) return;
        
    // Completed -> no timer actions.
    if (frm.doc.status === "Completed") return;

    const time_log = frm.doc.time_log || [];

    // Nothing logged yet -- the operation has not begun.
    if (!time_log.length) {
        frm.add_custom_button(__("Start"), () => start_jobs_dialog(frm), __("Job"));
        return;
    }

    // Paused: resuming is the only way on, the same as a Job Card on hold.
    if (frm.doc.status === "On Hold") {
        job_action_button(frm, __("Resume"), "resume_jobs");
        return;
    }

    if (time_log.some((t) => t.from_time && !t.to_time)) {
        frm.add_custom_button(__("Pause"), () => pause_job_dialog(frm), __("Job"));
        frm.add_custom_button(__("Complete"), () => complete_jobs_dialog(frm), __("Job"));
        return;
    }

    // Stopped but not on hold: pick the work back up, or close it out.
    job_action_button(frm, __("Resume"), "resume_jobs");
    frm.add_custom_button(__("Complete"), () => complete_jobs_dialog(frm), __("Job"));
}


function complete_jobs_dialog(frm) {
    const rows = qty_report_rows(frm);
    if (!rows.length) {
        frappe.msgprint(__("There are no linked Job Cards to complete."));
        return;
    }

    const d = new frappe.ui.Dialog({
        title: __("Complete Operation"),
        size: "extra-large",
        fields: [
            qty_report_grid(rows, [
                { fieldname: "completed_qty", label: __("Completed Quantity") },
                { fieldname: "pending_qty", label: __("Pending Quantity") },
                { fieldname: "process_loss_qty", label: __("Process Loss Quantity") },
            ]),
        ],
        primary_action_label: __("Complete"),
        primary_action(values) {
            const selected = values.rows || [];
            if (!valid_qty_report(selected, ["pending_qty", "process_loss_qty"], true)) return;

            d.hide();
            frm.call({
                method: "complete_jobs",
                doc: frm.doc,
                args: { rows: selected },
                freeze: true,
                freeze_message: __("Completing linked Job Cards..."),
            }).then(() => frm.reload_doc());
        },
    });
    d.show();
}


function job_action_button(frm, label, method) {
    frm.add_custom_button(label, () => {
        frm.call({
            method: method,
            doc: frm.doc,
            freeze: true,
            freeze_message: __("Processing linked Job Cards..."),
        }).then(() => frm.reload_doc());
    }, __("Job"));
}


function start_jobs_dialog(frm) {
    const d = new frappe.ui.Dialog({
        title: __("Assign Job to Employee"),
        fields: [
            {
                fieldtype: "Table MultiSelect",
                label: __("Select Employees"),
                options: "Master Job Card Employee",
                fieldname: "employees",
                reqd: 1,
            },
        ],
        primary_action_label: __("Start"),
        primary_action(values) {
            d.hide();
            frm.call({
                method: "start_jobs",
                doc: frm.doc,
                args: { employees: values.employees },
                freeze: true,
                freeze_message: __("Starting Job Cards..."),
            }).then(() => frm.reload_doc());
        },
    });
    d.show();
}


function pause_job_dialog(frm) {
    const rows = qty_report_rows(frm);
    if (!rows.length) {
        frappe.msgprint(__("There are no linked Job Cards to pause."));
        return;
    }

    const d = new frappe.ui.Dialog({
        title: __("Pause Operation"),
        size: "extra-large",
        fields: [
            {
                fieldtype: "Data",
                label: __("Reason"),
                fieldname: "reason",
                reqd: 1,
            },
            {
                fieldtype: "Section Break",
            },
            qty_report_grid(rows, [
                { fieldname: "completed_qty", label: __("Completed Quantity") },
                { fieldname: "rejected_qty", label: __("Rejected Quantity") },
            ]),
        ],
        primary_action_label: __("Pause"),
        primary_action(values) {
            const selected = values.rows || [];
            if (!valid_qty_report(selected, "rejected_qty")) return;

            d.hide();
            frm.call({
                method: "pause_jobs",
                doc: frm.doc,
                args: { reason: values.reason, rows: selected },
                freeze: true,
                freeze_message: __("Processing linked Job Cards..."),
            }).then(() => frm.reload_doc());
        },
    });
    d.show();
}


function qty_report_rows(frm) {
    return (frm.doc.job_card_detail || [])
        .filter((row) => row.job_card_number)
        .map((row) => {
            const ordered = flt(row.qty_to_manufacture);
            const done = flt(row.completed_qty);
            const accounted = done + flt(row.rejected_qty) + flt(row.process_loss_qty);

            return {
                job_card_number: row.job_card_number,
                item_code: row.item_code,
                qty_to_manufacture: ordered,
                already_completed: done,
                completed_qty: Math.max(ordered - accounted, 0),
                pending_qty: 0,
                rejected_qty: 0,
                process_loss_qty: 0,
            };
        });
}


function qty_report_grid(rows, editable) {
    return {
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
                fieldtype: "Float",
                fieldname: "qty_to_manufacture",
                label: __("Qty to Manufacture"),
                in_list_view: 1,
                read_only: 1,
                columns: 2,
            },
            {
                fieldtype: "Float",
                fieldname: "already_completed",
                label: __("Already Completed"),
                in_list_view: 1,
                read_only: 1,
                columns: 2,
            },
            ...editable.map((f) => ({
                fieldtype: "Float",
                fieldname: f.fieldname,
                label: f.label,
                in_list_view: 1,
                columns: 2,
            })),
            {
                fieldtype: "Data",
                fieldname: "job_card_number",
                label: __("Job Card"),
                hidden: 1,
            },
        ],
    };
}


function valid_qty_report(rows, extra_fields, exact) {
    const TOLERANCE = 0.001;
    const fields = Array.isArray(extra_fields) ? extra_fields : [extra_fields];

    for (const row of rows) {
        const ordered = flt(row.qty_to_manufacture);
        const reported = fields.reduce(
            (sum, f) => sum + flt(row[f]),
            flt(row.completed_qty),
        );
        const total = flt(row.already_completed) + reported;

        if (flt(row.completed_qty) < 0 || fields.some((f) => flt(row[f]) < 0)) {
            frappe.msgprint(__("Quantities cannot be negative."));
            return false;
        }

        if (total - ordered > TOLERANCE) {
            frappe.msgprint(
                __("{0}: {1} reported against a Qty to Manufacture of {2}. It cannot be more.", [
                    over_label(row),
                    format_number(total),
                    format_number(ordered),
                ]),
            );
            return false;
        }

        if (exact && ordered - total > TOLERANCE) {
            frappe.msgprint(
                __("{0}: only {1} of {2} is accounted for. Add the balance as Completed, Process Loss, or Pending to carry it to a new Master Job Card.", [
                    over_label(row),
                    format_number(total),
                    format_number(ordered),
                ]),
            );
            return false;
        }
    }

    return true;
}


function over_label(row) {
    return row.item_code || row.job_card_number;
}


function toggle_material_tab(frm) {
    const read_only = frm.doc.material_transfer_on === "Work Order";
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
