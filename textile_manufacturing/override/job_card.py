import frappe

from erpnext.manufacturing.doctype.job_card.job_card import JobCard


class CustomJobCard(JobCard):
    def get_open_job_cards(self, employee, workstation=None):
        """Do not count other Master-Job-Card-driven cards as the employee being busy.

        ERPNext treats any other *draft* Job Card carrying a time log for this employee
        as the employee working elsewhere -- see the base method, which compares no
        times at all, only that such a card exists. Fair enough when cards are
        independent and one operator means one workstation.

        That is not this shop. A Master Job Card runs an operation across every item in
        a lot at once, and several operations of a lot can be open together, so one
        operator legitimately appears on many draft cards. Left alone, the second card
        can never be completed:

            OverlapError: Employee 107 is currently working on another workstation.

        So cards raised by a Master Job Card are excluded from the check for each
        other. Anything raised outside that flow still counts -- if the operator is on
        an ordinary Job Card, that is a real clash and ERPNext should say so.
        """
        if self.driven_by_master_job_card():
            return []

        return super().get_open_job_cards(employee, workstation=workstation)

    def driven_by_master_job_card(self):
        return bool(
            frappe.db.get_value(
                "Master Job Card Detail", {"job_card_number": self.name}, "parent"
            )
        )
