from django.urls import path
from . import views
from .views import disconnect_youtube
urlpatterns = [
    path('register/', views.register_view, name='register'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('profile/', views.profile_view, name='profile'),
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('youtube/disconnect/<int:account_id>/', disconnect_youtube, name='disconnect_youtube'),
    path("about/", views.about_view, name="about"),
    path("privacy/", views.privacy_policy_view, name="privacy_policy"),
    path("terms/", views.terms_view, name="terms"),
    path("support/", views.support_view, name="support"),

]

