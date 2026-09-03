import uuid
from decimal import Decimal

from django.contrib.auth.hashers import make_password, check_password as check_password_hash
from django.db import models
from django.utils import timezone


class PhoneOTP(models.Model):
    """Short-lived OTP code used to verify a phone number before a trial
    account is created, to cut down on trial abuse."""
    phone_number = models.CharField(max_length=15, db_index=True)
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    verified = models.BooleanField(default=False)
    attempts = models.PositiveSmallIntegerField(default=0)

    OTP_VALIDITY_MINUTES = 5
    MAX_ATTEMPTS = 5

    def is_expired(self):
        return timezone.now() > self.created_at + timezone.timedelta(minutes=self.OTP_VALIDITY_MINUTES)

    def __str__(self):
        return f"OTP for {self.phone_number}"


class Router(models.Model):
    """A MikroTik router site. Supports multiple routers/branches."""
    name = models.CharField(max_length=100)
    host = models.GenericIPAddressField(help_text="Router IP address (LAN or VPN reachable)")
    api_port = models.PositiveIntegerField(default=8728, help_text="8728 plain, 8729 SSL")
    use_ssl = models.BooleanField(default=False)
    username = models.CharField(max_length=100)
    password = models.CharField(max_length=200)  # consider encrypting via django-fernet-fields in production
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} ({self.host})"


class Plan(models.Model):
    """A billing package, e.g. '5 Mbps Monthly' or 'Trial 7 Days'."""

    class ConnectionType(models.TextChoices):
        PPPOE = "pppoe", "PPPoE"
        HOTSPOT = "hotspot", "Hotspot"
        BOTH = "both", "Both"

    name = models.CharField(max_length=100)
    connection_type = models.CharField(max_length=10, choices=ConnectionType.choices, default=ConnectionType.BOTH)
    mikrotik_profile = models.CharField(
        max_length=100,
        help_text="Name of the PPP profile or Hotspot user profile on the router that grants this plan's speed"
    )
    download_speed_mbps = models.DecimalField(
        max_digits=6, decimal_places=2, default=0,
        help_text="Download speed in Mbps, e.g. 5.00. Pushed to MikroTik as the profile's rate-limit."
    )
    upload_speed_mbps = models.DecimalField(
        max_digits=6, decimal_places=2, blank=True, null=True,
        help_text="Upload speed in Mbps. Leave blank to use the same value as download."
    )
    price = models.DecimalField(max_digits=10, decimal_places=2)
    validity_days = models.PositiveIntegerField(default=30)
    is_trial = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} - KES {self.price} / {self.validity_days}d"

    @property
    def rate_limit_string(self):
        """MikroTik rate-limit format is 'upload/download', e.g. '5M/10M'."""
        upload = self.upload_speed_mbps or self.download_speed_mbps
        return f"{upload}M/{self.download_speed_mbps}M"


class Customer(models.Model):
    """An internet subscriber."""

    class ConnectionType(models.TextChoices):
        PPPOE = "pppoe", "PPPoE"
        HOTSPOT = "hotspot", "Hotspot"

    full_name = models.CharField(max_length=150)
    phone_number = models.CharField(max_length=15, unique=True, help_text="Format: 2547XXXXXXXX")
    email = models.EmailField(unique=True, help_text="Used to log in to the customer portal")
    password = models.CharField(
        max_length=128, blank=True, default="",
        help_text="Hashed portal login password (set via set_password()) — NOT the MikroTik password"
    )
    router = models.ForeignKey(Router, on_delete=models.PROTECT, related_name="customers")
    connection_type = models.CharField(max_length=10, choices=ConnectionType.choices)
    mikrotik_username = models.CharField(max_length=100, unique=True)
    mikrotik_password = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True, help_text="Whether the account is currently enabled on the router")
    date_joined = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.full_name} ({self.phone_number})"

    @property
    def current_subscription(self):
        return self.subscriptions.order_by("-expiry_date").first()

    def set_password(self, raw_password):
        """Hash and store the customer portal login password."""
        self.password = make_password(raw_password)

    def check_password(self, raw_password):
        """Verify a plaintext password against the stored hash."""
        if not self.password:
            return False
        return check_password_hash(raw_password, self.password)

    @property
    def login_allowed(self):
        """Portal login is only valid while the customer's current
        subscription (the 7-day trial, or a paid plan) hasn't expired.
        Once it lapses, login is refused — a lapsed trial customer is
        expected to register a fresh account rather than reuse this one."""
        sub = self.current_subscription
        return sub is not None and not sub.is_expired


class Subscription(models.Model):
    """Tracks a customer's active/expired billing period."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        SUSPENDED = "suspended", "Suspended"

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="subscriptions")
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT)
    start_date = models.DateTimeField(default=timezone.now)
    expiry_date = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    extended_by_admin = models.BooleanField(
        default=False, help_text="True if extended manually by an admin rather than via payment"
    )

    def __str__(self):
        return f"{self.customer} - {self.plan} (expires {self.expiry_date:%Y-%m-%d})"

    @property
    def is_expired(self):
        return timezone.now() >= self.expiry_date

    def extend(self, days, by_admin=False):
        base = self.expiry_date if self.expiry_date > timezone.now() else timezone.now()
        self.expiry_date = base + timezone.timedelta(days=days)
        self.status = self.Status.ACTIVE
        self.extended_by_admin = by_admin
        self.save()


class Payment(models.Model):
    """An M-Pesa payment record (STK push or C2B)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="payments")
    plan = models.ForeignKey(Plan, on_delete=models.SET_NULL, null=True, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    phone_number = models.CharField(max_length=15)
    merchant_request_id = models.CharField(max_length=100, blank=True, null=True)
    checkout_request_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    mpesa_receipt_number = models.CharField(max_length=50, blank=True, null=True, unique=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    raw_callback = models.JSONField(blank=True, null=True, help_text="Full Daraja callback payload for auditing")
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"{self.customer} - KES {self.amount} - {self.status}"


class Invoice(models.Model):
    """Auto-generated invoice per successful customer payment."""

    invoice_number = models.CharField(max_length=30, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="invoices")
    payment = models.OneToOneField(Payment, on_delete=models.CASCADE, related_name="invoice")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    issued_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.invoice_number

    def save(self, *args, **kwargs):
        if not self.invoice_number:
            self.invoice_number = f"INV-{timezone.now():%Y%m}-{uuid.uuid4().hex[:6].upper()}"
        super().save(*args, **kwargs)


class RevenueShareRecipient(models.Model):
    """The person/partner who receives the automatic 10% monthly earnings invoice."""
    name = models.CharField(max_length=150)
    email = models.EmailField(blank=True, null=True)
    phone_number = models.CharField(max_length=15, blank=True, null=True)
    share_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("10.00"))
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} ({self.share_percentage}%)"


class MonthlyRevenueInvoice(models.Model):
    """Auto-generated monthly invoice sent to the revenue share recipient."""
    recipient = models.ForeignKey(RevenueShareRecipient, on_delete=models.PROTECT, related_name="invoices")
    period_start = models.DateField()
    period_end = models.DateField()
    total_earnings = models.DecimalField(max_digits=12, decimal_places=2)
    share_amount = models.DecimalField(max_digits=12, decimal_places=2)
    generated_at = models.DateTimeField(auto_now_add=True)
    sent = models.BooleanField(default=False)
    sent_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        unique_together = ("recipient", "period_start", "period_end")

    def __str__(self):
        return f"{self.recipient} - {self.period_start:%b %Y} - KES {self.share_amount}"
