from django.urls import path
from . import views

urlpatterns = [
    path('subscribe/', views.subscribe_view, name='subscribe'),
    path('order/<str:plan_type>/', views.create_order, name='create_order'),
    path('callback/', views.payment_callback, name='payment_callback'),
    path('success/', views.payment_success, name='payment_success'),
    path('failed/', views.payment_failed, name='payment_failed'),
    path('cancel/<int:subscription_id>/', views.cancel_subscription, name='cancel_subscription'),
]
