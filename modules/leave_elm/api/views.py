from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from fusion_auth.permissions import HasModuleGrant, HasPermission
from modules.leave_elm.api import serializers as s
from modules.leave_elm.models import (
    ApprovalRecord,
    CancellationRecord,
    ExtensionRecord,
    HolidayCalendar,
    LeaveApplication,
    LeaveApplicationTransition,
    LeaveBalance,
    LeaveBalanceTransaction,
    LeaveCategory,
    OfflineSanctionedLeaveRecord,
    PolicyConfiguration,
    ResumptionRecord,
    Substitute,
)
from modules.leave_elm.services import review_application, submit_application

MODULE = HasModuleGrant("leave_elm")
P_VIEW = "leave_elm.application.view_self"
P_CREATE = "leave_elm.application.create"
P_WITHDRAW = "leave_elm.application.withdraw"
P_REVIEW = "leave_elm.application.review"


def _actor(request):
    return request.principal


def _own_application(request, pk):
    row = LeaveApplication.objects.filter(pk=pk, user_id=_actor(request).user_id).first()
    if row is None:
        raise NotFound("No such leave application.")
    return row


class LeaveApplicationListView(APIView):
    permission_classes = [MODULE, HasPermission(P_VIEW, P_CREATE)]

    def get(self, request):
        rows = LeaveApplication.objects.filter(
            user_id=_actor(request).user_id).select_related("category").order_by("-created_at")
        return Response({"results": s.LeaveApplicationSerializer(rows, many=True).data})

    def post(self, request):
        self.check_permissions(request)
        self.permission_classes = [MODULE, HasPermission(P_CREATE)]
        self.check_permissions(request)
        payload = s.LeaveApplicationCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        application = submit_application(user_id=_actor(request).user_id,
                                         **payload.validated_data)
        return Response(s.LeaveApplicationSerializer(application).data,
                        status=status.HTTP_201_CREATED)


class LeaveApplicationDetailView(APIView):
    permission_classes = [MODULE, HasPermission(P_VIEW)]

    def get(self, request, pk):
        application = _own_application(request, pk)
        history = application.transitions.order_by("at")
        decisions = application.approval_records.order_by("created_at")
        data = s.LeaveApplicationSerializer(application).data
        data["history"] = [{"from": row.from_status, "to": row.to_status,
                            "at": row.at, "reason": row.reason} for row in history]
        data["decisions"] = [{"outcome": row.outcome, "comments": row.comments,
                              "at": row.created_at} for row in decisions]
        return Response(data)


class LeaveBalanceListView(APIView):
    permission_classes = [MODULE, HasPermission(P_VIEW)]

    def get(self, request):
        rows = LeaveBalance.objects.filter(
            user_id=_actor(request).user_id).select_related("category").order_by("year", "category__code")
        return Response({"results": s.LeaveBalanceSerializer(rows, many=True).data})


class LeaveApplicationWithdrawView(APIView):
    permission_classes = [MODULE, HasPermission(P_WITHDRAW)]

    @transaction.atomic
    def post(self, request, pk):
        application = _own_application(request, pk)
        if application.status not in {
                "awaiting_substitute", "awaiting_unit_head", "awaiting_establishment",
                "awaiting_sanction", "awaiting_self_sanction", "applicant_action_required"}:
            raise ValidationError("Only a pending request can be withdrawn.")
        old = application.status
        application.status = "withdrawn"
        application.save(update_fields=["status", "updated_at"])
        LeaveApplicationTransition.objects.create(
            application=application, from_status=old, to_status="withdrawn",
            actor_user_id=_actor(request).user_id, actor_label="Employee")
        return Response(s.LeaveApplicationSerializer(application).data)


class SubstituteResponseView(APIView):
    permission_classes = [MODULE, HasPermission(P_VIEW)]

    def post(self, request, pk):
        payload = s.SubstituteResponseSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        assignment = Substitute.objects.filter(
            application_id=pk, substitute_user_id=_actor(request).user_id,
            status="pending", application__status="awaiting_substitute").select_related(
                "application").first()
        if assignment is None:
            raise NotFound("No pending substitute assignment.")
        response = payload.validated_data["response"]
        assignment.status = response
        assignment.response_at = timezone.now()
        assignment.save(update_fields=["status", "response_at", "updated_at"])
        application = assignment.application
        application.substitute_response = response
        application.status = ("awaiting_unit_head" if response == "accepted"
                              else "applicant_action_required")
        application.save(update_fields=["substitute_response", "status", "updated_at"])
        LeaveApplicationTransition.objects.create(
            application=application, from_status="awaiting_substitute",
            to_status=application.status, actor_user_id=_actor(request).user_id,
            actor_label="Substitute")
        return Response(s.LeaveApplicationSerializer(application).data)


class LeaveReviewView(APIView):
    permission_classes = [MODULE, HasPermission(P_REVIEW)]

    def post(self, request, pk):
        application = LeaveApplication.objects.filter(pk=pk).select_related(
            "category").first()
        if application is None:
            raise NotFound("No such leave application.")
        payload = s.ReviewSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        application = review_application(
            application=application, actor=_actor(request), **payload.validated_data)
        return Response(s.LeaveApplicationSerializer(application).data)


class LeaveCancellationView(APIView):
    permission_classes = [MODULE, HasPermission(P_CREATE)]

    def post(self, request, pk):
        application = _own_application(request, pk)
        if application.status != "approved" or application.starts_on <= timezone.localdate():
            raise ValidationError("Only approved leave that has not started can be cancelled.")
        payload = s.CancellationCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        row = CancellationRecord.objects.create(
            application=application, requested_by_user_id=_actor(request).user_id,
            **payload.validated_data)
        return Response({"id": row.pk, "status": row.status},
                        status=status.HTTP_201_CREATED)


class LeaveExtensionView(APIView):
    permission_classes = [MODULE, HasPermission(P_CREATE)]

    def post(self, request, pk):
        application = _own_application(request, pk)
        if application.category.code not in {"EL", "COL", "VL"} or application.status != "approved":
            raise ValidationError("Extensions are supported for ongoing EL, COL or VL.")
        payload = s.ExtensionCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        if data["additional_starts_on"] <= application.ends_on:
            raise ValidationError("The extension must start after the approved period.")
        days = (data["additional_ends_on"] - data["additional_starts_on"]).days + 1
        row = ExtensionRecord.objects.create(
            application=application, requested_by_user_id=_actor(request).user_id,
            additional_days=days, **data)
        return Response({"id": row.pk, "status": row.status},
                        status=status.HTTP_201_CREATED)


class LeaveResumptionView(APIView):
    permission_classes = [MODULE, HasPermission(P_CREATE)]

    def post(self, request, pk):
        application = _own_application(request, pk)
        if application.category.code not in {"EL", "COL", "VL"}:
            raise ValidationError("Resumption applies to EL, COL or VL.")
        payload = s.ResumptionCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        row = ResumptionRecord.objects.create(
            application=application, user_id=_actor(request).user_id,
            **payload.validated_data)
        return Response({"id": row.pk, "status": row.status},
                        status=status.HTTP_201_CREATED)


class ReplacementSubstituteView(APIView):
    permission_classes = [MODULE, HasPermission(P_CREATE)]

    def post(self, request, pk):
        application = _own_application(request, pk)
        if application.status != "applicant_action_required":
            raise ValidationError("The request is not awaiting a replacement substitute.")
        payload = s.ReplacementSubstituteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        substitute_id = payload.validated_data["substitute_user_id"]
        if substitute_id == application.user_id:
            raise ValidationError("You cannot nominate yourself as substitute.")
        Substitute.objects.filter(application=application, status="declined").update(
            status="replaced")
        Substitute.objects.create(application=application, user_id=application.user_id,
                                   substitute_user_id=substitute_id)
        application.substitute_user_id = substitute_id
        application.substitute_response = ""
        application.status = "awaiting_substitute"
        application.save(update_fields=["substitute_user_id", "substitute_response",
                                        "status", "updated_at"])
        LeaveApplicationTransition.objects.create(
            application=application, from_status="applicant_action_required",
            to_status="awaiting_substitute", actor_user_id=_actor(request).user_id,
            actor_label="Employee")
        return Response(s.LeaveApplicationSerializer(application).data)


class PolicyConfigurationView(APIView):
    permission_classes = [MODULE, HasPermission("leave_elm.configuration.manage")]

    def get(self, request):
        rows = PolicyConfiguration.objects.select_related("category").order_by("key")
        return Response({"results": s.PolicyConfigurationSerializer(rows, many=True).data})

    def post(self, request):
        payload = s.PolicyConfigurationSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        item = payload.save(updated_by_user_id=_actor(request).user_id)
        return Response(s.PolicyConfigurationSerializer(item).data,
                        status=status.HTTP_201_CREATED)


class HolidayCalendarView(APIView):
    permission_classes = [MODULE, HasPermission("leave_elm.configuration.manage")]

    def get(self, request):
        rows = HolidayCalendar.objects.order_by("-year", "name")
        return Response({"results": s.HolidayCalendarSerializer(rows, many=True).data})

    def post(self, request):
        payload = s.HolidayCalendarSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        calendar = payload.save()
        return Response(s.HolidayCalendarSerializer(calendar).data,
                        status=status.HTTP_201_CREATED)


class HolidayCalendarPublishView(APIView):
    permission_classes = [MODULE, HasPermission("leave_elm.configuration.manage")]

    def post(self, request, pk):
        calendar = HolidayCalendar.objects.filter(pk=pk).first()
        if calendar is None:
            raise NotFound("No such holiday calendar.")
        HolidayCalendar.objects.filter(year=calendar.year, is_published=True).exclude(
            pk=calendar.pk).update(is_published=False)
        calendar.is_published = True
        calendar.published_at = timezone.now()
        calendar.published_by_user_id = _actor(request).user_id
        calendar.save(update_fields=["is_published", "published_at",
                                     "published_by_user_id", "updated_at"])
        return Response(s.HolidayCalendarSerializer(calendar).data)


class OfflineSanctionedLeaveView(APIView):
    permission_classes = [MODULE, HasPermission("leave_elm.records.manage")]

    @transaction.atomic
    def post(self, request):
        payload = s.OfflineLeaveCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        if data["ends_on"] < data["starts_on"]:
            raise ValidationError("ends_on must not precede starts_on")
        category = data["category"]
        balance = LeaveBalance.objects.select_for_update().filter(
            user_id=data["user_id"], category=category,
            year=data["starts_on"].year).first()
        if balance is None:
            raise ValidationError("No leave balance exists for this employee and year.")
        record = OfflineSanctionedLeaveRecord.objects.create(
            **data, recorded_by_user_id=_actor(request).user_id)
        balance.used += record.leave_days
        balance.closing_balance -= record.leave_days
        balance.save(update_fields=["used", "closing_balance", "updated_at"])
        LeaveBalanceTransaction.objects.create(
            balance=balance, transaction_type="deduction", amount=record.leave_days,
            note=f"Offline sanction {record.sanction_reference}")
        return Response({"id": record.pk, "balance_id": balance.pk},
                        status=status.HTTP_201_CREATED)


class CancellationReviewView(APIView):
    permission_classes = [MODULE, HasPermission(P_REVIEW)]

    @transaction.atomic
    def post(self, request, pk):
        record = CancellationRecord.objects.select_for_update().filter(
            pk=pk, status="pending").select_related("application").first()
        if record is None:
            raise NotFound("No pending cancellation request.")
        payload = s.ReviewSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        outcome = payload.validated_data["outcome"]
        if outcome not in {"approved", "rejected"}:
            raise ValidationError("Cancellation requires a final decision.")
        record.status = outcome
        record.decided_by_user_id = _actor(request).user_id
        record.decided_at = timezone.now()
        record.save(update_fields=["status", "decided_by_user_id", "decided_at",
                                   "updated_at"])
        if outcome == "approved":
            application = record.application
            application.status = "cancelled"
            application.save(update_fields=["status", "updated_at"])
            balance = LeaveBalance.objects.select_for_update().get(
                user_id=application.user_id, category=application.category,
                year=application.starts_on.year)
            balance.used -= application.requested_days
            balance.closing_balance += application.requested_days
            balance.save(update_fields=["used", "closing_balance", "updated_at"])
            LeaveBalanceTransaction.objects.create(
                balance=balance, application=application,
                transaction_type="restoration", amount=application.requested_days)
        return Response({"id": record.pk, "status": record.status})


class ExtensionReviewView(APIView):
    permission_classes = [MODULE, HasPermission(P_REVIEW)]

    @transaction.atomic
    def post(self, request, pk):
        record = ExtensionRecord.objects.select_for_update().filter(
            pk=pk, status="pending").select_related(
                "application", "application__category").first()
        if record is None:
            raise NotFound("No pending extension request.")
        payload = s.ReviewSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        outcome = payload.validated_data["outcome"]
        if outcome not in {"approved", "rejected"}:
            raise ValidationError("Extension requires a final decision.")
        record.status = outcome
        record.decided_by_user_id = _actor(request).user_id
        record.decided_at = timezone.now()
        record.save(update_fields=["status", "decided_by_user_id", "decided_at",
                                   "updated_at"])
        if outcome == "approved":
            application = record.application
            balance = LeaveBalance.objects.select_for_update().get(
                user_id=application.user_id, category=application.category,
                year=record.additional_starts_on.year)
            balance.used += record.additional_days
            balance.closing_balance -= record.additional_days
            balance.save(update_fields=["used", "closing_balance", "updated_at"])
            LeaveBalanceTransaction.objects.create(
                balance=balance, application=application,
                transaction_type="deduction", amount=record.additional_days,
                note="Approved leave extension")
        return Response({"id": record.pk, "status": record.status})


class ResumptionReviewView(APIView):
    permission_classes = [MODULE, HasPermission(P_REVIEW)]

    @transaction.atomic
    def post(self, request, pk):
        record = ResumptionRecord.objects.select_for_update().filter(
            pk=pk, status="submitted").select_related("application").first()
        if record is None:
            raise NotFound("No submitted resumption record.")
        application = record.application
        actual_end = record.actual_resumption_date - timezone.timedelta(days=1)
        days = max((actual_end - application.starts_on).days + 1, 0)
        balance = LeaveBalance.objects.select_for_update().get(
            user_id=application.user_id, category=application.category,
            year=application.starts_on.year)
        difference = application.requested_days - days
        balance.used -= difference
        balance.closing_balance += difference
        balance.save(update_fields=["used", "closing_balance", "updated_at"])
        if difference:
            LeaveBalanceTransaction.objects.create(
                balance=balance, application=application,
                transaction_type="adjustment", amount=difference,
                note="Resumption review adjustment")
        record.actual_leave_days = days
        record.status = "reviewed"
        record.reviewed_by_user_id = _actor(request).user_id
        record.reviewed_at = timezone.now()
        record.save(update_fields=["actual_leave_days", "status", "reviewed_by_user_id",
                                   "reviewed_at", "updated_at"])
        return Response({"id": record.pk, "status": record.status,
                         "actual_leave_days": days})