"""Leave domain tables."""

from django.db import models

from core.db.mixins import TimeStampedModel, UserScopedModel


# -- Leave Category -------------------------------------------------------------
class LeaveCategory(TimeStampedModel):
    """A leave category (e.g., Casual Leave, Restricted Holiday)."""

    name = models.CharField(max_length=80)
    description = models.TextField(blank=True)

    class Meta:
        db_table = "leave_category"
        indexes = [models.Index(fields=["name"], name="category_name_idx")]

    def __str__(self) -> str:
        return self.name


# -- Leave Application ----------------------------------------------------------
class LeaveApplication(TimeStampedModel, UserScopedModel):
    STATUS = [
        ("draft", "Draft"), ("submitted", "Submitted"),
        ("under_review", "Under review"), ("approved", "Approved"),
        ("rejected", "Rejected"), ("withdrawn", "Withdrawn"),
        ("auto_withdrawn", "Withdrawn automatically"),
    ]

    employee = models.ForeignKey("Employee", on_delete=models.PROTECT,
                                 related_name="leave_applications")
    category = models.ForeignKey(LeaveCategory, on_delete=models.CASCADE,
                                 related_name="applications")
    status = models.CharField(max_length=24, choices=STATUS, default="draft")

    # Frozen at submission, so a later CPI change never rewrites history.
    cpi_at_apply = models.DecimalField(max_digits=4, decimal_places=2,
                                       null=True, blank=True)
    semester_at_apply = models.PositiveSmallIntegerField(null=True, blank=True)
    standing_declared_seq_at_apply = models.IntegerField(null=True, blank=True)
    eligibility_snapshot = models.JSONField(default=dict, blank=True)
    cover_note = models.TextField(blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "leave_application"
        constraints = [
            models.UniqueConstraint(fields=["employee_id", "category"],
                                    name="application_unique_per_employee_category"),
        ]
        indexes = [
            models.Index(fields=["employee_id", "status"], name="application_employee_idx"),
            models.Index(fields=["category", "status"], name="application_category_idx"),
        ]

    def __str__(self) -> str:
        return f"app#{self.pk} e{self.employee_id} {self.status}"


class LeaveApplicationTransition(models.Model):
    """Append-only audit of every status change (PC-BR-008)."""

    application = models.ForeignKey(LeaveApplication, on_delete=models.CASCADE,
                                    related_name="transitions")
    from_status = models.CharField(max_length=24)
    to_status = models.CharField(max_length=24)
    actor_user_id = models.IntegerField(null=True, blank=True)       # institute person
    actor_label = models.CharField(max_length=80, blank=True)
    reason = models.CharField(max_length=300, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "leave_application_transition"
        indexes = [models.Index(fields=["application", "at"],
                                name="transition_app_idx")]

    def __str__(self) -> str:
        return f"app#{self.application_id} {self.from_status}->{self.to_status}"


# -- Leave Balance -------------------------------------------------------------
class LeaveBalance(TimeStampedModel):
    """The current balance for an employee's leave category."""

    employee = models.OneToOneField("Employee", on_delete=models.CASCADE,
                                     related_name="leave_balance")

    category = models.ForeignKey(LeaveCategory, on_delete=models.CASCADE,
                                 related_name="balances")
    balance_at_start_of_period = models.DecimalField(max_digits=10, decimal_places=2)
    balance_after_end_of_period = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        db_table = "leave_balance"

    def __str__(self) -> str:
        return f"{self.employee_id}'s {self.category.name} balance"


# -- Leave Balance Transaction -------------------------------------------------
class LeaveBalanceTransaction(models.Model):
    """A transaction affecting the balance of a leave category (e.g., accrual)."""

    balance = models.ForeignKey(LeaveBalance, on_delete=models.CASCADE,
                                 related_name="transactions")
    type = models.CharField(max_length=24)  # e.g., 'accrue', 'use'
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "leave_balance_transaction"
        indexes = [models.Index(fields=["balance", "at"],
                                name="transaction_balance_idx")]

    def __str__(self) -> str:
        return f"t{self.pk}: {self.balance_id}, {self.type}, {self.amount}"


# -- Substitute ----------------------------------------------------------------
class Substitute(TimeStampedModel):
    """An employee who is substituting for another employee."""

    employee = models.OneToOneField("Employee", on_delete=models.CASCADE,
                                     related_name="substitute")

    substitute_for = models.ForeignKey("Employee", on_delete=models.PROTECT,
                                        related_name="substituted_by")
