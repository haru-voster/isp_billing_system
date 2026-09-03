from django.urls import path
from . import views

app_name = "billing"

urlpatterns = [
    path("register/", views.register_customer, name="register"),
    path("register/verify/", views.verify_registration, name="verify_registration"),
    path("register/resend-otp/", views.resend_otp, name="resend_otp"),

    path("login/", views.customer_login, name="customer_login"),
    path("logout/", views.customer_logout, name="customer_logout"),
    path("account/", views.customer_dashboard, name="customer_dashboard"),

    path("pay/", views.pay, name="pay"),
    path("mpesa/callback/", views.mpesa_callback, name="mpesa_callback"),
    path("intasend/webhook/", views.intasend_webhook, name="intasend_webhook"),
    path("portal/", views.portal_status, name="portal"),

    path("staff/login/", views.staff_login, name="staff_login"),
    path("staff/logout/", views.staff_logout, name="staff_logout"),
    path("staff/dashboard/", views.staff_dashboard, name="staff_dashboard"),

    path("staff/plans/", views.staff_plans, name="staff_plans"),
    path("staff/plans/new/", views.staff_plan_form, name="staff_plan_create"),
    path("staff/plans/<int:plan_id>/edit/", views.staff_plan_form, name="staff_plan_edit"),
    path("staff/plans/<int:plan_id>/resync/", views.staff_plan_resync, name="staff_plan_resync"),

    path("staff/routers/", views.staff_routers, name="staff_routers"),
    path("staff/routers/new/", views.staff_router_form, name="staff_router_create"),
    path("staff/routers/<int:router_id>/edit/", views.staff_router_form, name="staff_router_edit"),
]
