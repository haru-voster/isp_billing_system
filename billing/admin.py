from django.contrib import admin
from django.db.models import Sum
from django.utils import timezone

from .models import (
    Router, Plan, Customer, Subscription, Payment,
    Invoice, RevenueShareRecipient, MonthlyRevenueInvoice,
)


@admin.register(Router)
class RouterAdmin(admin.ModelAdmin):
    list_display = ("name", "host", "api_port", "is_active")


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("name", "connection_type", "price", "validity_days", "is_trial", "is_active")
    list_filter = ("connection_type", "is_trial", "is_active")


class SubscriptionInline(admin.TabularInline):
    model = Subscription
    extra = 0
    readonly_fields = ("start_date",)


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = (
        "full_name", "phone_number", "connection_type", "router",
        "is_active", "current_expiry", "monthly_spend",
    )
    list_filter = ("connection_type", "is_active", "router")
    search_fields = ("full_name", "phone_number", "mikrotik_username")
    inlines = [SubscriptionInline]
    actions = ["extend_30_days", "extend_7_days", "suspend_customers", "reactivate_customers"]

    @admin.display(description="Expires")
    def current_expiry(self, obj):
        sub = obj.current_subscription
        return sub.expiry_date if sub else "-"

    @admin.display(description="Spend this month (KES)")
    def monthly_spend(self, obj):
        now = timezone.now()
        total = obj.payments.filter(
            status=Payment.Status.SUCCESS,
            confirmed_at__year=now.year,
            confirmed_at__month=now.month,
        ).aggregate(total=Sum("amount"))["total"]
        return total or 0

    @admin.action(description="Extend selected customers by 30 days (admin override)")
    def extend_30_days(self, request, queryset):
        for customer in queryset:
            sub = customer.current_subscription
            if sub:
                sub.extend(30, by_admin=True)
                # Wire up MikroTik re-enable here, e.g.:
                # from .mikrotik import enable_customer
                # enable_customer(customer)

    @admin.action(description="Extend selected customers by 7 days (admin override)")
    def extend_7_days(self, request, queryset):
        for customer in queryset:
            sub = customer.current_subscription
            if sub:
                sub.extend(7, by_admin=True)

    @admin.action(description="Suspend selected customers on MikroTik")
    def suspend_customers(self, request, queryset):
        # from .mikrotik import disable_customer
        for customer in queryset:
            customer.is_active = False
            customer.save()
            # disable_customer(customer)

    @admin.action(description="Reactivate selected customers on MikroTik")
    def reactivate_customers(self, request, queryset):
        # from .mikrotik import enable_customer
        for customer in queryset:
            customer.is_active = True
            customer.save()
            # enable_customer(customer)


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("customer", "plan", "start_date", "expiry_date", "status", "extended_by_admin")
    list_filter = ("status", "plan", "extended_by_admin")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("customer", "amount", "phone_number", "status", "mpesa_receipt_number", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("mpesa_receipt_number", "phone_number", "checkout_request_id")
    readonly_fields = ("raw_callback",)


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("invoice_number", "customer", "amount", "issued_at")
    search_fields = ("invoice_number", "customer__full_name")


@admin.register(RevenueShareRecipient)
class RevenueShareRecipientAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "phone_number", "share_percentage", "is_active")


@admin.register(MonthlyRevenueInvoice)
class MonthlyRevenueInvoiceAdmin(admin.ModelAdmin):
    list_display = ("recipient", "period_start", "period_end", "total_earnings", "share_amount", "sent")
    list_filter = ("sent", "recipient")
