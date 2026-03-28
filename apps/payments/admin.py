from django.contrib import admin
from .models import Subscription, Payment


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    readonly_fields = ('razorpay_payment_id', 'amount', 'status', 'method', 'created_at')
    can_delete = False


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ('user', 'plan_type', 'status', 'max_streams', 'start_date', 'end_date', 'is_active')
    list_filter = ('plan_type', 'status', 'is_active', 'start_date')
    search_fields = ('user__username', 'razorpay_order_id', 'razorpay_payment_id')
    readonly_fields = ('created_at', 'updated_at', 'start_date')
    inlines = [PaymentInline]
    
    fieldsets = (
        ('User & Plan', {
            'fields': ('user', 'plan_type', 'max_streams')
        }),
        ('Status', {
            'fields': ('status', 'is_active')
        }),
        ('Razorpay Details', {
            'fields': ('razorpay_order_id', 'razorpay_payment_id', 'razorpay_signature', 'amount')
        }),
        ('Dates', {
            'fields': ('start_date', 'end_date', 'created_at', 'updated_at')
        }),
    )


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('subscription', 'razorpay_payment_id', 'amount', 'status', 'method', 'created_at')
    list_filter = ('status', 'method', 'created_at')
    search_fields = ('razorpay_payment_id', 'subscription__user__username')
    readonly_fields = ('created_at',)
