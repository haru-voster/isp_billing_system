from django import forms

from .models import Plan, Router


class PlanForm(forms.ModelForm):
    class Meta:
        model = Plan
        fields = [
            "name", "connection_type", "mikrotik_profile",
            "download_speed_mbps", "upload_speed_mbps",
            "price", "validity_days", "is_trial", "is_active",
        ]
        widgets = {
            "mikrotik_profile": forms.TextInput(attrs={
                "placeholder": "e.g. 5mbps-monthly (created automatically on save if it doesn't exist)"
            }),
        }
        help_texts = {
            "mikrotik_profile": "This exact profile name will be created/updated on every active router.",
        }


class RouterForm(forms.ModelForm):
    class Meta:
        model = Router
        fields = ["name", "host", "api_port", "use_ssl", "username", "password", "is_active"]
        widgets = {
            "password": forms.PasswordInput(render_value=True),
        }
