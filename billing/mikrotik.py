"""
MikroTik RouterOS API helpers.

Uses the RouterOS API (port 8728/8729) via `librouteros` — NOT Winbox.
Winbox is a Windows GUI tool for manual router administration; it has no
programmatic interface, so all automated actions here talk to the same
API that tools like Winbox/WebFig use under the hood, just from Python.

Install: pip install librouteros
"""
import logging

from librouteros import connect
from librouteros.exceptions import TrapError

logger = logging.getLogger(__name__)


def _get_api(router):
    """Open a connection to a Router model instance."""
    return connect(
        username=router.username,
        password=router.password,
        host=router.host,
        port=router.api_port,
    )


def create_pppoe_user(customer, plan):
    """Create a PPPoE secret for a new customer."""
    router = customer.router
    api = _get_api(router)
    try:
        api.path("ppp", "secret").add(
            name=customer.mikrotik_username,
            password=customer.mikrotik_password,
            service="pppoe",
            profile=plan.mikrotik_profile,
        )
    finally:
        api.close()


def create_hotspot_user(customer, plan):
    """Create a Hotspot user for a new customer."""
    router = customer.router
    api = _get_api(router)
    try:
        api.path("ip", "hotspot", "user").add(
            name=customer.mikrotik_username,
            password=customer.mikrotik_password,
            profile=plan.mikrotik_profile,
        )
    finally:
        api.close()


def _find_id(api, path_parts, name):
    resource = api.path(*path_parts)
    for row in resource:
        if row.get("name") == name:
            return row[".id"]
    return None


def set_customer_profile(customer, profile_name):
    """Switch a customer to a given profile (e.g. active plan, or a 'suspended'
    walled-garden profile with zero/limited bandwidth)."""
    router = customer.router
    api = _get_api(router)
    try:
        if customer.connection_type == customer.ConnectionType.PPPOE:
            path_parts = ("ppp", "secret")
        else:
            path_parts = ("ip", "hotspot", "user")

        item_id = _find_id(api, path_parts, customer.mikrotik_username)
        if item_id is None:
            logger.warning("MikroTik user %s not found on router %s", customer.mikrotik_username, router)
            return False

        api.path(*path_parts).update(**{".id": item_id, "profile": profile_name})
        return True
    except TrapError:
        logger.exception("MikroTik API error updating profile for %s", customer.mikrotik_username)
        return False
    finally:
        api.close()


def disable_customer(customer, suspended_profile="suspended"):
    """Disable internet access — moves the user to a suspended/walled-garden profile
    (recommended) so they still see a 'please pay' captive portal page instead of
    just losing the connection silently."""
    return set_customer_profile(customer, suspended_profile)


def enable_customer(customer):
    """Re-enable a customer by switching them back to their current plan's profile."""
    sub = customer.current_subscription
    if not sub:
        logger.warning("No subscription found for %s, cannot re-enable", customer)
        return False
    return set_customer_profile(customer, sub.plan.mikrotik_profile)


def remove_pppoe_user(customer):
    router = customer.router
    api = _get_api(router)
    try:
        item_id = _find_id(api, ("ppp", "secret"), customer.mikrotik_username)
        if item_id:
            api.path("ppp", "secret").remove(item_id)
    finally:
        api.close()


def remove_hotspot_user(customer):
    router = customer.router
    api = _get_api(router)
    try:
        item_id = _find_id(api, ("ip", "hotspot", "user"), customer.mikrotik_username)
        if item_id:
            api.path("ip", "hotspot", "user").remove(item_id)
    finally:
        api.close()


# ---------------------------------------------------------------------------
# Plan provisioning — creates or updates the PPP profile / hotspot user
# profile on the router so a plan's speed (Mbps) is actually enforced,
# instead of requiring you to manually create matching profiles in Winbox.
# ---------------------------------------------------------------------------

def create_or_update_ppp_profile(router, profile_name, rate_limit):
    """Create the PPP profile if it doesn't exist, or update its rate-limit if it does."""
    api = _get_api(router)
    try:
        item_id = _find_id(api, ("ppp", "profile"), profile_name)
        if item_id:
            api.path("ppp", "profile").update(**{".id": item_id, "rate-limit": rate_limit})
        else:
            api.path("ppp", "profile").add(**{"name": profile_name, "rate-limit": rate_limit})
        return True
    except TrapError:
        logger.exception("MikroTik API error creating/updating PPP profile %s on %s", profile_name, router)
        return False
    finally:
        api.close()


def create_or_update_hotspot_profile(router, profile_name, rate_limit):
    """Create the Hotspot user profile if it doesn't exist, or update its rate-limit if it does."""
    api = _get_api(router)
    try:
        item_id = _find_id(api, ("ip", "hotspot", "user", "profile"), profile_name)
        if item_id:
            api.path("ip", "hotspot", "user", "profile").update(**{".id": item_id, "rate-limit": rate_limit})
        else:
            api.path("ip", "hotspot", "user", "profile").add(**{"name": profile_name, "rate-limit": rate_limit})
        return True
    except TrapError:
        logger.exception("MikroTik API error creating/updating hotspot profile %s on %s", profile_name, router)
        return False
    finally:
        api.close()


def provision_plan_on_router(router, plan):
    """
    Push a plan's speed to a specific router as a matching PPP/hotspot profile.
    Returns (success: bool, error_message: str|None).
    """
    rate_limit = plan.rate_limit_string
    try:
        if plan.connection_type in (plan.ConnectionType.PPPOE, plan.ConnectionType.BOTH):
            ok = create_or_update_ppp_profile(router, plan.mikrotik_profile, rate_limit)
            if not ok:
                return False, f"Failed to sync PPP profile on {router.name}"
        if plan.connection_type in (plan.ConnectionType.HOTSPOT, plan.ConnectionType.BOTH):
            ok = create_or_update_hotspot_profile(router, plan.mikrotik_profile, rate_limit)
            if not ok:
                return False, f"Failed to sync hotspot profile on {router.name}"
        return True, None
    except Exception as exc:
        logger.exception("Unexpected error provisioning plan %s on router %s", plan, router)
        return False, f"Could not reach {router.name}: {exc}"


def provision_plan_on_all_routers(plan):
    """Push a plan's profile/speed to every active router. Returns a list of
    (router_name, success, error_message) so the caller can show per-router results."""
    from .models import Router  # local import avoids a circular import at module load time

    results = []
    for router in Router.objects.filter(is_active=True):
        success, error = provision_plan_on_router(router, plan)
        results.append((router.name, success, error))
    return results
