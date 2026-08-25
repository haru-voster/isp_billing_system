import os
from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ispbilling.settings")

app = Celery("ispbilling")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    "suspend-expired-subscriptions-every-15-min": {
        "task": "billing.tasks.suspend_expired_subscriptions",
        "schedule": crontab(minute="*/15"),
    },
    "poll-pending-intasend-payments": {
        "task": "billing.tasks.poll_pending_intasend_payments",
        "schedule": crontab(minute="*/3"),  # every 3 minutes — fallback in case a webhook is missed
    },
    "generate-monthly-revenue-invoice": {
        "task": "billing.tasks.generate_monthly_revenue_invoice",
        "schedule": crontab(minute=0, hour=6, day_of_month=1),  # 6am on the 1st of each month
    },
}
