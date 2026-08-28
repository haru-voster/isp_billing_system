from django.core.management.base import BaseCommand
from billing.tasks import generate_monthly_revenue_invoice


class Command(BaseCommand):
    help = (
        "Compute last month's revenue share invoice(s) and email them. "
        "Run this once a month (e.g. 6am on the 1st) via a cPanel cron job."
    )

    def handle(self, *args, **options):
        invoice_ids = generate_monthly_revenue_invoice()
        self.stdout.write(self.style.SUCCESS(f"Generated {len(invoice_ids)} monthly revenue invoice(s)."))
