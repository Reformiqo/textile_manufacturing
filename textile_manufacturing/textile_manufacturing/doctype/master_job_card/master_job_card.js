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


// Time Log grid: Time in Mins = To Time - From Time (in minutes).
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
    // Material transfer is done from the Master Job Card only when material is
    // transferred on the Job Card (not on the Work Order), and after it is saved.
    if (!frm.doc.docstatus === 1 || frm.doc.material_transfer_on !== "Work Oder") return;

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
    if (!frm.doc.quality_inspection_requied || frm.doc.docstatus !== 1) return;

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
    if (frm.doc.docstatus !== 1 || (frm.doc.job_card_detail || []).every((r) => !r.job_card_number)) return;
        
    // Not started yet
    let time_log = frm.doc.time_log || [];
    if(frm.doc.time_log.length == 0){
        frm.add_custom_button(__("Start"), () => start_jobs_dialog(frm), __("Job"));
        return;
    }
    
    // Completed -> no timer actions.
    if (frm.doc.status === "Completed") return;

    // A row with from_time but no to_time means a timer is currently running.
    const running = time_log.some((t) => t.from_time && !t.to_time);

    if (running) {
        frm.add_custom_button(__("Pause"), () => pause_job_dialog(frm), __("Job"));
    } else {
        [["Resume", "resume_jobs"], ["Complete", "complete_jobs"]].forEach(([label, method]) => {
            frm.add_custom_button(__(label), () => {
                frm.call({
                    method: method,
                    doc: frm.doc,
                    freeze: true,
                    freeze_message: __("Processing linked Job Cards..."),
                }).then(() => frm.reload_doc());
            }, __("Job"));
        });
    }
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
    const d = new frappe.ui.Dialog({
        title: __("Reason for Pause"),
        fields: [
            {
                fieldtype: "Data",
                label: __("Reason"),
                fieldname: "reason",
                reqd: 1,
            },
        ],
        primary_action_label: __("Pause"),
        primary_action(values) {
            d.hide();
            frm.call({
                method: "pause_jobs",
                doc: frm.doc,
                args: { reason: values.reason },
                freeze: true,
                freeze_message: __("Processing linked Job Cards..."),
            }).then(() => frm.reload_doc());
        },
    });
    d.show();
}


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
