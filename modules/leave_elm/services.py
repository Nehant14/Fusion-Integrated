"""Basic leave validation and account updates."""

from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from modules.leave_elm.models import (
    ApprovalRecord,
    LeaveApplication,
    LeaveApplicationTransition,
    LeaveBalance,
    LeaveBalanceTransaction,
    LeaveCategory,
)


CONTINUOUS_CODES = {"EL", "COL", "VL"}
ACTIVE_STATUSES = {
    "awaiting_substitute", "awaiting_unit_head", "awaiting_establishment",
    "awaiting_sanction", "awaiting_self_sanction", "applicant_action_required",
    "approved",
}


def _calendar_days(start, end):
    return [start + timedelta(days=offset)
            for offset in range((end - start).days + 1)]


def chargeable_days(category, start, end, half_day="full"):
    days = _calendar_days(start, end)
    if category.code not in CONTINUOUS_CODES:
        days = [day for day in days if day.weekday() < 5]
    amount = Decimal(len(days))
    if start == end and half_day != "full":
        return Decimal("0.5")
    return amount


def validate_application(*, user_id, category, starts_on, ends_on, half_day):
    if not category.is_active:
        raise ValidationError("This leave category is not active.")
    amount = chargeable_days(category, starts_on, ends_on, half_day)
    if amount <= 0:
        raise ValidationError("The requested period contains no chargeable leave days.")

    for existing in LeaveApplication.objects.filter(
            user_id=user_id, status__in=ACTIVE_STATUSES,
            starts_on__lte=ends_on, ends_on__gte=starts_on):
        same_day_cl_halves = (
            category.code == "CL" and existing.category.code == "CL"
            and starts_on == ends_on == existing.starts_on == existing.ends_on
            and half_day != "full" and existing.half_day != "full"
            and half_day != existing.half_day)
        if not same_day_cl_halves:
            raise ValidationError("The requested period overlaps another active leave.")

    balance = LeaveBalance.objects.filter(
        user_id=user_id, category=category, year=starts_on.year).first()
    if balance is None or balance.closing_balance < amount:
        raise ValidationError("Insufficient available leave balance.")
    return amount


@transaction.atomic
def submit_application(*, user_id, **data):
    category = LeaveCategory.objects.get(pk=data.pop("category_id"), is_active=True)
    amount = validate_application(
        user_id=user_id, category=category, starts_on=data["starts_on"],
        ends_on=data["ends_on"], half_day=data["half_day"])
    substitute_id = data.get("substitute_user_id")
    if substitute_id == user_id:
        raise ValidationError("You cannot nominate yourself as substitute.")
    status = "awaiting_substitute" if substitute_id else "awaiting_unit_head"
    application = LeaveApplication.objects.create(
        user_id=user_id, category=category, requested_days=amount,
        status=status, submitted_at=timezone.now(), **data)
    LeaveApplicationTransition.objects.create(
        application=application, from_status="draft", to_status=status,
        actor_user_id=user_id, actor_label="Employee")
    if substitute_id:
        from modules.leave_elm.models import Substitute
        Substitute.objects.create(application=application, user_id=user_id,
                                  substitute_user_id=substitute_id)
    return application


@transaction.atomic
def review_application(*, application, actor, outcome, comments=""):
    if application.status not in {
            "awaiting_unit_head", "awaiting_establishment", "awaiting_sanction",
            "awaiting_self_sanction"}:
        raise ValidationError("This application is not awaiting a decision.")
    category = application.category
    old_status = application.status
    if category.code in {"CL", "RH"}:
        if outcome not in {"approved", "rejected"}:
            raise ValidationError("CL/RH review must be a final decision.")
    elif old_status == "awaiting_unit_head":
        if outcome not in {"recommended", "not_recommended"}:
            raise ValidationError("Unit Head must record a recommendation.")
    elif outcome not in {"approved", "rejected"}:
        raise ValidationError("Final authority must approve or reject.")

    ApprovalRecord.objects.create(
        application=application, actor_user_id=actor.user_id,
        actor_role=actor.active_role or "", outcome=outcome, comments=comments)
    if outcome == "recommended" or outcome == "not_recommended":
        application.status = "awaiting_sanction"
    else:
        application.status = outcome
    application.save(update_fields=["status", "updated_at"])
    LeaveApplicationTransition.objects.create(
        application=application, from_status=old_status,
        to_status=application.status, actor_user_id=actor.user_id,
        actor_label=actor.active_role or "", reason=comments)
    if application.status == "approved":
        balance = LeaveBalance.objects.select_for_update().get(
            user_id=application.user_id, category=category,
            year=application.starts_on.year)
        balance.used += application.requested_days
        balance.closing_balance -= application.requested_days
        balance.save(update_fields=["used", "closing_balance", "updated_at"])
        LeaveBalanceTransaction.objects.create(
            balance=balance, application=application,
            transaction_type="deduction", amount=application.requested_days)
    return application