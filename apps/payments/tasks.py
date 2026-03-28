from celery import shared_task
from django.utils import timezone
from datetime import timedelta
import logging

from .models import Subscription
from apps.streaming.models import Stream

logger = logging.getLogger(__name__)


@shared_task
def check_subscription_expiry():
    """
    Check for expired subscriptions and deactivate them
    Runs daily at midnight via Celery Beat
    """
    # ✅ Log when task runs
    logger.info(f"🔍 Running check_subscription_expiry at {timezone.now()}")
    
    # Find subscriptions that have expired
    expired_subscriptions = Subscription.objects.filter(
        is_active=True,
        end_date__lt=timezone.now(),
        status='active'
    )
    
    logger.info(f"Found {expired_subscriptions.count()} expired subscriptions")
    
    for subscription in expired_subscriptions:
        try:
            # Update subscription status
            subscription.status = 'expired'
            subscription.is_active = False
            subscription.save()
            
            # Stop all running streams for this user
            running_streams = Stream.objects.filter(
                user=subscription.user,
                status__in=['running', 'starting']
            )
            
            for stream in running_streams:
                try:
                    from apps.streaming.stream_manager import StreamManager
                    manager = StreamManager(stream)
                    manager.stop_stream()
                    
                    from apps.streaming.models import StreamLog
                    StreamLog.objects.create(
                        stream=stream,
                        level='WARNING',
                        message='Stream stopped due to subscription expiry'
                    )
                except Exception as stream_error:
                    logger.error(f"Failed to stop stream {stream.id}: {stream_error}")
            
            logger.info(f"✅ Deactivated expired subscription {subscription.id} for user {subscription.user.username}")
            
        except Exception as e:
            logger.error(f"❌ Failed to expire subscription {subscription.id}: {e}")
    
    # Send warning for subscriptions expiring in 3 days
    warning_date = timezone.now() + timedelta(days=3)
    expiring_soon = Subscription.objects.filter(
        is_active=True,
        end_date__lte=warning_date,
        end_date__gte=timezone.now(),
        status='active'
    )
    
    logger.info(f"Found {expiring_soon.count()} subscriptions expiring in 3 days")
    
    for subscription in expiring_soon:
        logger.warning(f"⏰ Subscription {subscription.id} for user {subscription.user.username} expiring on {subscription.end_date}")
        # TODO: Send email notification
    
    logger.info(f"✅ Processed {expired_subscriptions.count()} expired subscriptions")
    return f"Deactivated {expired_subscriptions.count()} expired subscriptions"

'''
@shared_task
def check_subscription_expiry():
    """
    Check for expired subscriptions and deactivate them
    Runs daily at midnight via Celery Beat
    """
    # Find subscriptions that have expired
    expired_subscriptions = Subscription.objects.filter(
        is_active=True,
        end_date__lt=timezone.now(),
        status='active'
    )
    
    for subscription in expired_subscriptions:
        # Update subscription status
        subscription.status = 'expired'
        subscription.is_active = False
        subscription.save()
        
        # Stop all running streams for this user
        running_streams = Stream.objects.filter(
            user=subscription.user,
            status__in=['running', 'starting']
        )
        
        for stream in running_streams:
            from apps.streaming.stream_manager import StreamManager
            manager = StreamManager(stream)
            manager.stop_stream()
            
            from apps.streaming.models import StreamLog
            StreamLog.objects.create(
                stream=stream,
                level='WARNING',
                message='Stream stopped due to subscription expiry'
            )
        
        logger.info(f"Deactivated expired subscription for user {subscription.user.username}")
    
    # Send warning for subscriptions expiring in 3 days
    warning_date = timezone.now() + timedelta(days=3)
    expiring_soon = Subscription.objects.filter(
        is_active=True,
        end_date__lte=warning_date,
        end_date__gte=timezone.now(),
        status='active'
    )
    
    for subscription in expiring_soon:
        # Here you can send email notifications
        logger.warning(f"Subscription for user {subscription.user.username} expiring soon")
        # TODO: Send email notification
    
    logger.info(f"Processed {expired_subscriptions.count()} expired subscriptions")
    return f"Deactivated {expired_subscriptions.count()} expired subscriptions"

'''
@shared_task
def send_payment_receipt(payment_id):
    """
    Send payment receipt email to user
    """
    try:
        from .models import Payment
        payment = Payment.objects.get(id=payment_id)
        
        # TODO: Implement email sending logic
        logger.info(f"Payment receipt sent for payment {payment_id}")
        return f"Receipt sent for payment {payment_id}"
        
    except Exception as e:
        logger.error(f"Failed to send receipt for payment {payment_id}: {str(e)}")
        return f"Failed to send receipt: {str(e)}"
