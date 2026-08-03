// An order behind a Master Work Order sends the goods themselves out to be worked
// on, so there is no raw material to supply -- the server clears the table, this
// keeps it out of the way.
frappe.ui.form.on("Subcontracting Order", {
    refresh: function (frm) {
        toggle_supplied_items(frm);
        add_material_to_supplier_button(frm);
    },

    master_work_order: function (frm) {
        toggle_supplied_items(frm);
    },
});


// ERPNext offers its own Material to Supplier button only while Supplied Items still
// has something owing. That table is empty here, so the button never appears -- this
// puts it back, sending the finished goods out instead.
function add_material_to_supplier_button(frm) {
    if (!frm.doc.master_work_order) return;
    if (frm.doc.docstatus !== 1 || frm.doc.status === "Closed") return;

    frm.add_custom_button(__("Material to Supplier"), () => {
        frappe.call({
            method: "erpnext.controllers.subcontracting_controller.make_rm_stock_entry",
            args: {
                subcontract_order: frm.doc.name,
                order_doctype: frm.doc.doctype,
            },
            freeze: true,
            freeze_message: __("Building the transfer entry..."),
            callback: (r) => {
                if (!r.message) return;
                const doclist = frappe.model.sync(r.message);
                frappe.set_route("Form", doclist[0].doctype, doclist[0].name);
            },
        });
    }, __("Create"));
}


function toggle_supplied_items(frm) {
    const hide = !!frm.doc.master_work_order;

    ["raw_materials_supplied_section", "supplied_items"].forEach((fieldname) => {
        if (frm.fields_dict[fieldname]) {
            frm.set_df_property(fieldname, "hidden", hide ? 1 : 0);
        }
    });
}
