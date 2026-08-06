frappe.ui.form.on("Production Plan", {
    refresh(frm) {
        // Remove standard buttons
        frm.remove_custom_button(__("Work Order / Subcontract PO"), __("Create"));

        if (frm.doc.status !== "Completed" && frm.doc.docstatus == 1) {
            let items = frm.events.get_items_for_work_order(frm);
            if (items?.length && frm.doc.status !== "Closed") {
					frm.add_custom_button(__("Master Work Order"),
                    () => {
                        make_master_word_order(frm);
                    },
                    __("Create")
                );
   
                frm.page.set_inner_btn_group_as_primary(__("Create"));
            }
        }
    }
});


function make_master_word_order(frm){
    frappe.call({
        method: "textile_manufacturing.textile_manufacturing.doctype.master_work_order.master_work_order.make_master_work_order",
        freeze: true,
        args: {
            production_plan_id : frm.doc.name
        },
        callback: function () {
            frappe.show_alert({ message: __("Master Work Order Created"), indicator: "green" });
            frm.reload_doc();
        },
    });
}