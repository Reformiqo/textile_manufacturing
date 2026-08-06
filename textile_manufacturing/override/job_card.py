import frappe
from frappe.utils import add_to_date, get_datetime

# time_diff_in_minutes is defined in ERPNext's job_card module, not in frappe.utils.
from erpnext.manufacturing.doctype.job_card.job_card import JobCard, time_diff_in_minutes


class CustomJobCard(JobCard):
    """Job Cards raised by a Master Job Card are scheduled by that flow, not by
    ERPNext's shop-floor rules.

    ERPNext assumes a Job Card is an independent unit of work: one operator at one
    workstation, inside working hours, in operation sequence. A Master Job Card breaks
    every one of those assumptions on purpose -- it runs an operation across every item
    in a lot at once, several operations of a lot may be open together, and a bulk start
    happens whenever the supervisor presses the button, night shift included.

    So the scheduling checks are bypassed for cards under that flow, and left exactly as
    ERPNext wrote them for any Job Card raised outside it.
    """

    def driven_by_master_job_card(self):
        # Looked up once per document -- these checks run per time log row.
        if self.flags.get("master_driven") is None:
            self.flags.master_driven = bool(
                self.name
                and frappe.db.get_value(
                    "Master Job Card Detail", {"job_card_number": self.name}, "parent"
                )
            )

        return self.flags.master_driven

    def get_open_job_cards(self, employee, workstation=None):
        """Whether the operator is "busy elsewhere".

        The base method compares no times at all -- any other draft Job Card carrying a
        time log for this employee counts. One operator working a whole lot across many
        cards would disqualify every one of them:

            OverlapError: Employee 107 is currently working on another workstation.
        """
        if self.driven_by_master_job_card():
            return []

        return super().get_open_job_cards(employee, workstation=workstation)

    def get_overlap_for(self, args, open_job_cards=None):
        """Workstation and time-slot overlap.

        Several cards of one operation deliberately run on the same workstation at the
        same moment here, so overlapping is the normal state rather than a clash."""
        if self.driven_by_master_job_card():
            return {}

        return super().get_overlap_for(args, open_job_cards=open_job_cards)

    def validate_sequence_id(self):
        """Operation order.

        ERPNext refuses an operation whose predecessor has no completed qty --
        "complete the operation X before the operation Y". The lot is worked as a
        whole here and operations are completed together, so the order is the
        supervisor's call rather than the system's."""
        if self.driven_by_master_job_card():
            return

        return super().validate_sequence_id()

    def check_workstation_time(self, row):
        """Working hours, holiday list and shift.

        A bulk start would otherwise fail outside the workstation's hours or on a
        holiday, which is exactly when a textile floor tends to be running. The
        scheduling below is ERPNext's own path for a workstation with no working hours
        defined -- lay the operation end to end from its start time, nothing more."""
        if not self.driven_by_master_job_card():
            return super().check_workstation_time(row)

        if get_datetime(row.planned_end_time) <= get_datetime(row.planned_start_time):
            row.planned_end_time = add_to_date(
                row.planned_start_time, minutes=row.time_in_mins
            )
            row.remaining_time_in_mins = 0.0
        else:
            row.remaining_time_in_mins -= time_diff_in_minutes(
                row.planned_end_time, row.planned_start_time
            )

        self.update_time_logs(row)
