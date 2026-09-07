app_name = "textile_manufacturing"
app_title = "Textile Manufacturing"
app_publisher = "Reformiqo"
app_description = "Custom App for Textile Manufacturing"
app_email = "consultant.reformiqo@gmail.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "textile_manufacturing",
# 		"logo": "/assets/textile_manufacturing/logo.png",
# 		"title": "Textile Manufacturing",
# 		"route": "/textile_manufacturing",
# 		"has_permission": "textile_manufacturing.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/textile_manufacturing/css/textile_manufacturing.css"
# app_include_js = "/assets/textile_manufacturing/js/textile_manufacturing.js"

# include js, css files in header of web template
# web_include_css = "/assets/textile_manufacturing/css/textile_manufacturing.css"
# web_include_js = "/assets/textile_manufacturing/js/textile_manufacturing.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "textile_manufacturing/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
doctype_js = {
    "Production Plan" : "public/js/production_plan.js",
    "Subcontracting Order" : "public/js/subcontracting_order.js"
}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "textile_manufacturing/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "textile_manufacturing.utils.jinja_methods",
# 	"filters": "textile_manufacturing.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "textile_manufacturing.install.before_install"
# after_install = "textile_manufacturing.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "textile_manufacturing.uninstall.before_uninstall"
# after_uninstall = "textile_manufacturing.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "textile_manufacturing.utils.before_app_install"
# after_app_install = "textile_manufacturing.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "textile_manufacturing.utils.before_app_uninstall"
# after_app_uninstall = "textile_manufacturing.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "textile_manufacturing.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "textile_manufacturing.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
	"Work Order": {
		"on_update": "textile_manufacturing.override.work_order.on_update",
	},
	"Stock Entry": {
		"on_submit": [
            "textile_manufacturing.override.stock_entry.update_master_work_order_returns",
            "textile_manufacturing.override.stock_entry.update_master_work_order_consumed"
        ],
		"on_cancel": [
            "textile_manufacturing.override.stock_entry.update_master_work_order_returns",
            "textile_manufacturing.override.stock_entry.update_master_work_order_consumed",
            # Cancel only -- the Finish does its own recount on the way out, and
            # hooking the submit as well runs it part way through a Finish.
            "textile_manufacturing.override.stock_entry.update_master_work_order_manufactured"
        ],
	},
	"Purchase Order": {
		"validate": "textile_manufacturing.override.purchase_order.keep_fg_qty_in_step",
		# The operation line the order was raised for carries its number, so the
		# operations table says which work is away at a supplier and on what.
		"on_submit": "textile_manufacturing.override.purchase_order.link_operation_to_purchase_order",
		"on_cancel": "textile_manufacturing.override.purchase_order.link_operation_to_purchase_order",
	},
	"Subcontracting Order": {
		"validate": "textile_manufacturing.override.subcontracting_order.set_master_work_order",
	},
	"Subcontracting Receipt": {
		"validate": "textile_manufacturing.override.subcontracting_receipt.set_receipt_master_work_order",
		# The receipt is what carries the Subcontracting Order to Completed, which is
		# what an Out House Master Work Order waits on before it can be finished.
		"on_submit": "textile_manufacturing.override.subcontracting_receipt.update_master_work_order_status",
		"on_cancel": "textile_manufacturing.override.subcontracting_receipt.update_master_work_order_status",
	},
	"Quality Inspection": {
		"on_submit": "textile_manufacturing.override.quality_inspection.update_master_job_card_detail",
		"on_cancel": "textile_manufacturing.override.quality_inspection.update_master_job_card_detail"
	},
}

# Create/refresh this app's custom fields on migrate.
after_migrate = "textile_manufacturing.custom_fields.make_custom_fields"


override_whitelisted_methods = {
	"erpnext.controllers.subcontracting_controller.make_rm_stock_entry": "textile_manufacturing.override.subcontracting_order.make_rm_stock_entry",
}


override_doctype_class = {
    "Work Order": "textile_manufacturing.override.work_order.CustomWorkOrder",
	"Job Card": "textile_manufacturing.override.job_card.CustomJobCard",
	"Stock Entry": "textile_manufacturing.override.stock_entry.CustomStockEntry",
	"Production Plan": "textile_manufacturing.override.production_plan.CustomProductionPlan",
}

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"textile_manufacturing.tasks.all"
# 	],
# 	"daily": [
# 		"textile_manufacturing.tasks.daily"
# 	],
# 	"hourly": [
# 		"textile_manufacturing.tasks.hourly"
# 	],
# 	"weekly": [
# 		"textile_manufacturing.tasks.weekly"
# 	],
# 	"monthly": [
# 		"textile_manufacturing.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "textile_manufacturing.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "textile_manufacturing.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "textile_manufacturing.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
override_doctype_dashboards = {
	"Production Plan": "textile_manufacturing.override.production_plan_dashboard.get_data"
}

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["textile_manufacturing.utils.before_request"]
# after_request = ["textile_manufacturing.utils.after_request"]

# Job Events
# ----------
# before_job = ["textile_manufacturing.utils.before_job"]
# after_job = ["textile_manufacturing.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"textile_manufacturing.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

