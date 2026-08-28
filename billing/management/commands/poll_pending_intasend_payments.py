from django.core.management.base import BaseCommand
from billing.tasks import poll_pending_intasend_payments


class Command(BaseCommand):
    help = (
        "Check IntaSend directly for any payment still marked pending, in case a "
        "webhook was missed. Run this every 3-5 minutes via a cPanel cron job."
    )

    def handle(self, *args, **options):
        updated = poll_pending_intasend_payments()
        self.stdout.write(self.style.SUCCESS(f"Updated {updated} pending payment(s)."))
