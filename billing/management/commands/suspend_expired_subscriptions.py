from django.core.management.base import BaseCommand
from billing.tasks import suspend_expired_subscriptions


class Command(BaseCommand):
    help = (
        "Suspend any customer whose subscription (trial or paid) has expired. "
        "Run this every 15 minutes via a cPanel cron job — no Celery/Redis needed."
    )

    def handle(self, *args, **options):
        count = suspend_expired_subscriptions()
        self.stdout.write(self.style.SUCCESS(f"Suspended {count} expired subscription(s)."))
