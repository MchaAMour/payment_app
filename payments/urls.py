from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from . import views

urlpatterns = [
    path("", views.store_page),
    path("dashboard/", views.dashboard),
    path("login/", views.login_page),
    path("checkout/", views.checkout_page),
    path("auth/register", views.register),
    path("auth/token", TokenObtainPairView.as_view()),
    path("auth/token/refresh", TokenRefreshView.as_view()),
    path("v1/me", views.me),
    path("v1/dashboard", views.dashboard_data),
    path("demo/start", views.demo_start),
    path("v1/payments", views.create_payment),
    path("v1/payments/<str:payment_order_id>", views.get_payment),
    path("webhooks/stripe/", views.stripe_webhook),
]
