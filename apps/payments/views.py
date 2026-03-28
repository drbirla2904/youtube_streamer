from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponseBadRequest, JsonResponse
from django.conf import settings
import razorpay
from .models import Subscription, Payment
import logging
logger = logging.getLogger(__name__)
razorpay_client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

@login_required
def subscribe_view(request):
    """Show subscription plans"""
    plans = settings.SUBSCRIPTION_PLANS

    # Check if user has active subscription
    active_subscription = Subscription.objects.filter(
        user=request.user,
        is_active=True,
        status='active'
    ).first()

    context = {
        'plans': plans,
        'active_subscription': active_subscription,
        'razorpay_key_id': settings.RAZORPAY_KEY_ID,
    }

    return render(request, 'payments/subscribe.html', context)

@login_required
def create_order(request, plan_type):
    """Create Razorpay order with plan hierarchy check and storage limit"""

    if plan_type not in settings.SUBSCRIPTION_PLANS:
        messages.error(request, 'Invalid plan type selected.')
        return redirect('subscribe')

    try:
        plans = settings.SUBSCRIPTION_PLANS
        plan = plans[plan_type]
        amount = plan['price']

        active_subscription = Subscription.objects.filter(
            user=request.user,
            is_active=True,
            status='active'
        ).first()

        plan_priority = {
            'oneday': 0,
            'monthly': 1,
            'annual': 2,
        }

        if active_subscription:
            active_plan_type = active_subscription.plan_type
            if plan_priority.get(plan_type, 0) <= plan_priority.get(active_plan_type, 0):
                messages.warning(
                    request,
                    f'You already have an active {active_plan_type.title()} plan. '
                    f'Downgrading to a lower plan is not allowed.'
                )
                return redirect('subscribe')

        razorpay_order = razorpay_client.order.create({
            'amount': amount,
            'currency': 'INR',
            'payment_capture': '1'
        })

        # ✅ Create subscription - will calculate end_date on first save
        subscription = Subscription.objects.create(
            user=request.user,
            plan_type=plan_type,
            razorpay_order_id=razorpay_order['id'],
            amount=amount,
            status='pending',  # ✅ Set to 'pending' until payment confirmed
            is_active=False  # ✅ Not active until payment confirmed
        )
        # ✅ This save() will trigger calculate_and_set_end_date()

        context = {
            'razorpay_order_id': razorpay_order['id'],
            'razorpay_key_id': settings.RAZORPAY_KEY_ID,
            'amount': amount,
            'currency': 'INR',
            'plan_name': plan['name'],
            'subscription_id': subscription.id,
        }

        return render(request, 'payments/checkout.html', context)

    except Exception as e:
        messages.error(request, f'Failed to create order: {str(e)}')
        return redirect('subscribe')
@csrf_exempt
def payment_callback(request):
    """Handle Razorpay payment callback"""
    if request.method == "POST":
        try:
            payment_id = request.POST.get('razorpay_payment_id', '')
            razorpay_order_id = request.POST.get('razorpay_order_id', '')
            signature = request.POST.get('razorpay_signature', '')

            params_dict = {
                'razorpay_order_id': razorpay_order_id,
                'razorpay_payment_id': payment_id,
                'razorpay_signature': signature
            }

            # Verify payment signature
            razorpay_client.utility.verify_payment_signature(params_dict)

            # Get subscription
            subscription = Subscription.objects.get(razorpay_order_id=razorpay_order_id)

            # ✅ Verify end_date is set before activating
            if not subscription.end_date:
                from django.utils import timezone
                from datetime import timedelta
                plan_config = settings.SUBSCRIPTION_PLANS.get(subscription.plan_type)
                if plan_config:
                    duration_days = plan_config.get('duration_days', 30)
                    subscription.end_date = timezone.now() + timedelta(days=duration_days)
                    subscription.save()

            # Update subscription
            subscription.razorpay_payment_id = payment_id
            subscription.razorpay_signature = signature
            subscription.is_active = True  # ✅ Now activate
            subscription.status = 'active'  # ✅ Now mark as active
            subscription.save()
            
            logger.info(
                f"✅ Subscription {subscription.id} activated. "
                f"Ends: {subscription.end_date}"
            )

            # Deactivate other subscriptions
            Subscription.objects.filter(
                user=subscription.user
            ).exclude(id=subscription.id).update(is_active=False)

            # Create payment record
            payment_details = razorpay_client.payment.fetch(payment_id)
            Payment.objects.create(
                subscription=subscription,
                razorpay_payment_id=payment_id,
                amount=subscription.amount,
                status=payment_details.get('status', 'success'),
                method=payment_details.get('method', '')
            )

            messages.success(request, 'Subscription activated successfully!')
            return redirect('payment_success')

        except razorpay.errors.SignatureVerificationError:
            logger.error("Signature verification failed")  # ✅ Now works
            messages.error(request, 'Payment verification failed')
            return redirect('payment_failed')
        except Exception as e:
            logger.error(f"Payment callback error: {e}")
            messages.error(request, f'Payment processing failed: {str(e)}')
            return redirect('payment_failed')

    return HttpResponseBadRequest()


'''
@login_required
def create_order(request, plan_type):
    """Create Razorpay order with plan hierarchy check and storage limit"""

    # Validate plan type
    if plan_type not in settings.SUBSCRIPTION_PLANS:
        messages.error(request, 'Invalid plan type selected.')
        return redirect('subscribe')

    try:
        # Fetch all plans and selected plan details
        plans = settings.SUBSCRIPTION_PLANS
        plan = plans[plan_type]
        amount = plan['price']  # Price in paise

        # Get the user's active subscription, if any
        active_subscription = Subscription.objects.filter(
            user=request.user,
            is_active=True,
            status='active'
        ).first()

        # Optional: define plan ranking hierarchy (higher ⇒ better)
        plan_priority = {
            'oneday': 0,
            'monthly': 1,
            'annual': 2,
        }

        # Check user is already on equal or higher plan
        if active_subscription:
            active_plan_type = active_subscription.plan_type
            if plan_priority.get(plan_type, 0) <= plan_priority.get(active_plan_type, 0):
                messages.warning(
                    request,
                    f'You already have an active {active_plan_type.title()} plan. '
                    f'Downgrading to a lower plan is not allowed.'
                )
                return redirect('subscribe')

        # Create new Razorpay order for valid upgrade
        razorpay_order = razorpay_client.order.create({
            'amount': amount,
            'currency': 'INR',
            'payment_capture': '1'
        })

        # NEW: Calculate storage limit based on plan
        if plan_type == 'monthly':
            storage_limit = 1 * (1024 ** 3)  # 1GB in bytes
        elif plan_type == 'annual':
            storage_limit = 2 * (1024 ** 3)  # 2GB in bytes
        else:
            storage_limit = 0.5 * (1024 ** 3)  # Default 0.5GB

        # Create subscription record
        subscription = Subscription.objects.create(
            user=request.user,
            plan_type=plan_type,
            razorpay_order_id=razorpay_order['id'],
            amount=amount,
            max_streams=plan['max_streams'],
            storage_limit=storage_limit,  # NEW: Store the storage limit
            status='active',
            is_active=False  # will activate after payment confirmation
        )

        context = {
            'razorpay_order_id': razorpay_order['id'],
            'razorpay_key_id': settings.RAZORPAY_KEY_ID,
            'amount': amount,
            'currency': 'INR',
            'plan_name': plan['name'],
            'subscription_id': subscription.id,
        }

        return render(request, 'payments/checkout.html', context)

    except Exception as e:
        messages.error(request, f'Failed to create order: {str(e)}')
        return redirect('subscribe')

@csrf_exempt
def payment_callback(request):
    """Handle Razorpay payment callback"""
    if request.method == "POST":
        try:
            payment_id = request.POST.get('razorpay_payment_id', '')
            razorpay_order_id = request.POST.get('razorpay_order_id', '')
            signature = request.POST.get('razorpay_signature', '')

            params_dict = {
                'razorpay_order_id': razorpay_order_id,
                'razorpay_payment_id': payment_id,
                'razorpay_signature': signature
            }

            # Verify payment signature
            razorpay_client.utility.verify_payment_signature(params_dict)

            # Get subscription
            subscription = Subscription.objects.get(razorpay_order_id=razorpay_order_id)

            # Update subscription
            subscription.razorpay_payment_id = payment_id
            subscription.razorpay_signature = signature
            subscription.is_active = True
            subscription.status = 'active'
            subscription.save()

            # Deactivate other subscriptions
            Subscription.objects.filter(
                user=subscription.user
            ).exclude(id=subscription.id).update(is_active=False)

            # Create payment record
            payment_details = razorpay_client.payment.fetch(payment_id)
            Payment.objects.create(
                subscription=subscription,
                razorpay_payment_id=payment_id,
                amount=subscription.amount,
                status=payment_details.get('status', 'success'),
                method=payment_details.get('method', '')
            )

            messages.success(request, 'Subscription activated successfully!')
            return redirect('payment_success')

        except razorpay.errors.SignatureVerificationError:
            messages.error(request, 'Payment verification failed')
            return redirect('payment_failed')
        except Exception as e:
            messages.error(request, f'Payment processing failed: {str(e)}')
            return redirect('payment_failed')

    return HttpResponseBadRequest()
'''
@login_required
def payment_success(request):
    """Payment success page"""
    return render(request, 'payments/payment_success.html')

@login_required
def payment_failed(request):
    """Payment failed page"""
    return render(request, 'payments/payment_failed.html')

@login_required
def cancel_subscription(request, subscription_id):
    """Cancel a subscription"""
    subscription = Subscription.objects.get(id=subscription_id, user=request.user)
    subscription.status = 'cancelled'
    subscription.is_active = False
    subscription.save()

    messages.success(request, 'Subscription cancelled successfully')
    return redirect('dashboard')
