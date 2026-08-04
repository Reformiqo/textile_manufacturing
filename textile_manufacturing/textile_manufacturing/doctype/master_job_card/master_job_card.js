// Copyright (c) 2026, Reformiqo and contributors
// For license information, please see license.txt

frappe.ui.form.on("Master Job Card", {
    refresh: function (frm) {
        if (frm.doc.master_work_order_number) {
            load_inhouse_operations(frm);
        }
        toggle_material_tab(frm);
        add_quality_inspection_button(frm);
        add_sfg_button(frm);
        set_job_card_dashboard(frm);
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


// Stock Out puts the goods into store (a receipt); 
// Stock In hands them back to the floor to be worked (a consumption).
const SFG_ENTRY = {
    "Stock Out": {
        title: "SFG Stock Out -- Material Receipt",
        warehouse_label: "Target Warehouse",
    },
    "Stock In": {
        title: "SFG Stock In -- Material Consumption for Manufacture",
        warehouse_label: "Source Warehouse",
    },
};


function add_sfg_button(frm) {
    if (frm.doc.docstatus !== 1) return;

    frm.call({ method: "sfg_item_rows", doc: frm.doc }).then((r) => {
        const rows = r.message || [];
        if (!rows.length) return;

        Object.keys(SFG_ENTRY).forEach((entry_type) => {
            frm.add_custom_button(__("SFG {0}", [entry_type]), () => {
                sfg_stock_dialog(frm, entry_type, rows);
            }, __("Create"));
        });
    });
}


function sfg_stock_dialog(frm, entry_type, source_rows) {
    const config = SFG_ENTRY[entry_type];
    // A copy per dialog -- both buttons are handed the same list.
    const rows = source_rows.map((row) => ({ ...row }));

    const d = new frappe.ui.Dialog({
        title: __(config.title),
        size: "large",
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
                        columns: 4,
                    },
                    {
                        fieldtype: "Link",
                        fieldname: "warehouse",
                        label: __(config.warehouse_label),
                        options: "Warehouse",
                        in_list_view: 1,
                        reqd: 1,
                        columns: 4,
                    },
                    {
                        fieldtype: "Float",
                        fieldname: "qty",
                        label: __("Qty"),
                        in_list_view: 1,
                        reqd: 1,
                        columns: 2,
                    },
                    {
                        fieldtype: "Data",
                        fieldname: "uom",
                        label: __("UOM"),
                        hidden: 1,
                    },
                ],
            },
        ],
        primary_action_label: __("Create"),
        primary_action(values) {
            const selected = (values.rows || []).filter((row) => flt(row.qty) > 0);
            if (!selected.length) {
                frappe.msgprint(__("Enter a Qty for at least one item."));
                return;
            }

            d.hide();
            frm.call({
                method: "make_sfg_stock_entry",
                doc: frm.doc,
                args: { entry_type: entry_type, rows: selected },
                freeze: true,
                freeze_message: __("Creating Stock Entry..."),
            }).then(() => frm.reload_doc());
        },
    });
    d.show();
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
                { fieldname: "rejected_qty", label: __("Rejected Quantity") },
                { fieldname: "process_loss_qty", label: __("Process Loss Quantity") },
            ]),
        ],
        primary_action_label: __("Complete"),
        primary_action(values) {
            const selected = values.rows || [];
            if (!valid_qty_report(frm, selected, true)) return;

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
                label: __("Pause Reason"),
                fieldname: "reason",
                reqd: 1,
            },
            {
                fieldtype: "Section Break",
            },
            qty_report_grid(rows, [
                { fieldname: "completed_qty", label: __("Completed Quantity") },
                { fieldname: "rejected_qty", label: __("Rejected Quantity") },
                {
                    fieldname: "rejection_reason",
                    label: __("Rejection Reason"),
                    fieldtype: "Data",
                    columns: 3,
                },
            ]),
        ],
        primary_action_label: __("Pause"),
        primary_action(values) {
            const selected = values.rows || [];
            if (!valid_qty_report(frm, selected)) return;
            if (!valid_rejection_reason(selected)) return;

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

            return {
                job_card_number: row.job_card_number,
                item_code: row.item_code,
                qty_to_manufacture: ordered,
                completed_qty: Math.max(ordered - consumed_qty(row), 0),
                pending_qty: 0,
                rejected_qty: 0,
                process_loss_qty: 0,
                rejection_reason: row.rejection_reason || "",
            };
        });
}


function valid_rejection_reason(rows) {
    const missing = rows.filter(
        (row) => flt(row.rejected_qty) > 0 && !(row.rejection_reason || "").trim()
    );
    if (!missing.length) return true;

    frappe.msgprint({
        title: __("Rejection Reason Missing"),
        message: __("Enter a Rejection Reason for: {0}", [
            missing.map((row) => over_label(row)).join(", "),
        ]),
        indicator: "red",
    });
    return false;
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
                columns: 2,
            },
            {
                fieldtype: "Float",
                fieldname: "qty_to_manufacture",
                label: __("Qty to Manufacture"),
                in_list_view: 1,
                columns: 2,
            },
            ...editable.map((f) => ({
                fieldtype: f.fieldtype || "Float",
                fieldname: f.fieldname,
                label: f.label,
                in_list_view: 1,
                columns: f.columns || 2,
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


function valid_qty_report(frm, rows, exact) {
    const detail = {};
    (frm.doc.job_card_detail || []).forEach((row) => {
        if (row.job_card_number) detail[row.job_card_number] = row;
    });

    for (const row of rows) {
        const source = detail[row.job_card_number] || {};
        const ordered = flt(source.qty_to_manufacture);
        const total = accounted_qty(row);

        if (ACCOUNTED_FIELDS.some((f) => flt(row[f]) < 0)) {
            frappe.msgprint(__("Quantities cannot be negative."));
            return false;
        }

        if (total > ordered) {
            frappe.msgprint(
                __("{0}: {1} reported against a Qty to Manufacture of {2}. It cannot be more.", [
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


const ACCOUNTED_FIELDS = ["completed_qty", "process_loss_qty", "rejected_qty"];
function accounted_qty(row) {
    return ACCOUNTED_FIELDS.reduce((sum, f) => sum + flt(row[f]), 0);
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


function set_job_card_dashboard(frm) {
	// Submitted cards still show the widget -- read-only, for the total time.
	if (frm.is_new() || frm.doc.docstatus === 2) return;
	if ((frm.doc.job_card_detail || []).every((r) => !r.job_card_number)) return;

	const wrapper = $(frm.fields_dict["job_card_dashboard"].wrapper);
	wrapper.empty();

	// Clear any previous timer tick before re-rendering.
	if (frm._job_timer_interval) {
		clearInterval(frm._job_timer_interval);
		frm._job_timer_interval = null;
	}

	// Completed -> no timer actions, no widget at all.
	// Trust either the status field, OR the actual quantities (in case status
	// hasn't flipped yet, or the time log wasn't auto-closed server-side).
	const all_rows_done = (frm.doc.job_card_detail || [])
		.filter((row) => row.job_card_number)
		.every((row) => {
			const ordered = flt(row.qty_to_manufacture);
			const accounted =
				flt(row.completed_qty) + flt(row.rejected_qty) + flt(row.process_loss_qty);
			return ordered - accounted <= 0.001;
		});

	// Done -- the total time stands as a record, with nothing left to press and
	// nothing left to count.
	if (frm.doc.status === "Completed" || frm.doc.docstatus === 1 || all_rows_done) {
		render_job_timer_widget(wrapper, {
			label: __("Total Time"),
			seconds: flt(frm.doc.total_actual_time) * 60,
			buttons_html: "",
		});
		return;
	}

	const time_log = frm.doc.time_log || [];
	const running_log = all_rows_done ? null : time_log.find((t) => t.from_time && !t.to_time);

	// ── Decide which buttons to show (same branches as the toolbar-button version) ──
	let show_start = false,
		show_resume = false,
		show_pause = false,
		show_complete = false;

	if (!time_log.length) {
		// Nothing logged yet -- the operation has not begun.
		show_start = true;
	} else if (frm.doc.status === "On Hold") {
		// Paused: resuming is the only way on.
		show_resume = true;
	} else if (running_log) {
		show_pause = true;
		show_complete = true;
	} else {
		// Stopped but not on hold: pick work back up, or close it out.
		show_resume = true;
		show_complete = true;
	}

	const is_timer_running = !!running_log;

	// ── Build HTML ──────────────────────────────────────────────────────
	const btn = (cls, label) =>
		`<button class="btn btn-sm ${cls}" style="font-weight:600;padding:6px 14px;">${label}</button>`;

	const buttons_html = [
		show_start && btn("btn-primary jt-btn-start", __("Start")),
		show_resume && btn("btn-primary jt-btn-resume", __("Resume")),
		show_pause && btn("btn-default jt-btn-pause", __("Pause")),
		show_complete && btn("btn-primary jt-btn-complete", __("Complete")),
	]
		.filter(Boolean)
		.join(" ");

	render_job_timer_widget(wrapper, {
		label: __("Elapsed Time"),
		seconds: 0,
		buttons_html: buttons_html,
	});

	// ── Bind click handlers (only after the HTML exists in the DOM) ─────
	if (show_start) {
		wrapper.find(".jt-btn-start").on("click", () => start_jobs_dialog(frm));
	}
	if (show_resume) {
		wrapper.find(".jt-btn-resume").on("click", () => {
			frm.call({
				method: "resume_jobs",
				doc: frm.doc,
				freeze: true,
				freeze_message: __("Processing linked Job Cards..."),
			}).then(() => frm.reload_doc());
		});
	}
	if (show_pause) {
		wrapper.find(".jt-btn-pause").on("click", () => pause_job_dialog(frm));
	}
	if (show_complete) {
		wrapper.find(".jt-btn-complete").on("click", () => complete_jobs_dialog(frm));
	}

	// ── Stopwatch tick, only while a log entry is actually open ─────────
	if (!is_timer_running) return;

	const timer_el = wrapper.find(".jt-stopwatch");
	let elapsed = Math.floor(
		(frappe.datetime.str_to_obj(frappe.datetime.now_datetime()) -
			frappe.datetime.str_to_obj(running_log.from_time)) /
			1000
	);
	if (isNaN(elapsed) || elapsed < 0) elapsed = 0;

	timer_el.text(format_stopwatch(elapsed));
	frm._job_timer_interval = setInterval(() => {
		elapsed += 1;
		timer_el.text(format_stopwatch(elapsed));
	}, 1000);
}


function format_stopwatch(seconds) {
	const pad = (n) => String(n).padStart(2, "0");
	const h = Math.floor(seconds / 3600);
	const m = Math.floor((seconds % 3600) / 60);
	const s = Math.floor(seconds % 60);
	return `${pad(h)}:${pad(m)}:${pad(s)}`;
}


function render_job_timer_widget(wrapper, { label, seconds, buttons_html }) {
	wrapper.append(`
		<div class="job-timer-dashboard-widget"
			style="border:1px solid var(--border-color);border-radius:var(--border-radius-lg,8px);
				background:var(--card-bg,#fff);padding:16px 20px;margin-bottom:16px;">
			<div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;">
				<div>
					<div style="font-size:10px;color:var(--text-muted);font-weight:600;
						text-transform:uppercase;letter-spacing:0.6px;margin-bottom:6px;">
						${label}
					</div>
					<span class="jt-stopwatch"
						style="font-family:var(--monospace-font,'Courier New',monospace);
						font-size:24px;font-weight:700;letter-spacing:2px;">
						${format_stopwatch(seconds)}
					</span>
				</div>
				<div style="display:flex;gap:8px;flex-wrap:wrap;">
					${buttons_html}
				</div>
			</div>
		</div>`);
}