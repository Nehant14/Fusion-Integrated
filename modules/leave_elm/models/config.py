"""Current leave policy, holiday and workflow timing configuration."""

from django.db import models

from core.db.mixins import TimeStampedModel


class PolicyConfiguration(TimeStampedModel):
    """Current parameters; policy configuration has no publish lifecycle."""

    category = models.ForeignKey("leave_elm.LeaveCategory", null=True, blank=True,
                                 on_delete=models.CASCADE,
                                 related_name="policy_configurations")
    key = models.CharField(max_length=80)
    value = models.JSONField(default=dict, blank=True)
    updated_by_user_id = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = "leave_policy_configuration"
        constraints = [models.UniqueConstraint(
            fields=["category", "key"], name="leave_policy_category_key_uniq")]


class HolidayCalendar(TimeStampedModel):
    name = models.CharField(max_length=120)
    year = models.PositiveSmallIntegerField()
    holidays = models.JSONField(default=list, blank=True)
    restricted_holidays = models.JSONField(default=list, blank=True)
    is_published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by_user_id = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = "leave_holiday_calendar"
        constraints = [models.UniqueConstraint(
            fields=["year", "name"], name="leave_holiday_year_name_uniq")]


class OfflineSanctionedLeaveRecord(TimeStampedModel):
    user_id = models.IntegerField(db_index=True)
    category = models.ForeignKey("leave_elm.LeaveCategory",
                                 on_delete=models.PROTECT)
    starts_on = models.DateField()
    ends_on = models.DateField()
    leave_days = models.DecimalField(max_digits=7, decimal_places=2)
    purpose = models.TextField(blank=True)
    sanction_reference = models.CharField(max_length=200)
    evidence_reference = models.CharField(max_length=500, blank=True)
    recorded_by_user_id = models.IntegerField()

    class Meta:
        db_table = "leave_offline_sanctioned_record"


class SLAConfiguration(TimeStampedModel):
    workflow_name = models.CharField(max_length=80, unique=True)
    reminder_hours = models.PositiveIntegerField()
    escalation_hours = models.PositiveIntegerField()
    escalation_role = models.CharField(max_length=80, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "leave_sla_configuration"