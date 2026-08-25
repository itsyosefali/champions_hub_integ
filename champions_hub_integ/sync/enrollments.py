import frappe
import requests
from champions_hub_integ.sync.mapper import upsert_enrollment, log_enrollment_error


def sync_enrollments():
    """Hourly scheduled job: incremental sync from Champions Hub API."""
    settings = frappe.get_single("Champions Hub Settings")
    if not settings.enabled:
        return

    token = settings.get_password("api_token")
    if not token:
        frappe.log_error("Champions Hub: API token not configured", "Champions Hub Sync")
        return

    base_url = (settings.base_url or "https://champions-hub.com/api/v1").rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}

    cursor = settings.last_synced_at
    page = 1
    last_page = 1
    high_water = cursor

    statuses = "active,refunded,chargeback"
    gateways = "stripe,xpay,instapay,vodafone_cash,manual_transfer"

    try:
        while page <= last_page:
            params = {
                "page": page,
                "per_page": 200,
                "status": statuses,
                "payment_gateway": gateways,
            }
            if cursor:
                params["updated_since"] = cursor

            resp = requests.get(
                f"{base_url}/integrations/enrollments",
                headers=headers,
                params=params,
                timeout=60,
            )

            if resp.status_code == 429:
                frappe.log_error(
                    f"Champions Hub: Rate limited at page {page}. Will resume next run.",
                    "Champions Hub Sync",
                )
                break

            resp.raise_for_status()
            body = resp.json()

            for row in body["data"]:
                try:
                    upsert_enrollment(row, settings)
                except Exception as e:
                    frappe.db.rollback()
                    try:
                        log_enrollment_error(row, str(e))
                        frappe.db.commit()
                    except Exception:
                        frappe.db.rollback()
                    frappe.log_error(
                        frappe.get_traceback(),
                        f"Champions Hub: Error processing enrollment {row.get('id')}",
                    )

            meta = body["meta"]
            last_page = meta["last_page"]
            page_high = meta.get("max_updated_at")
            if page_high:
                if not high_water or page_high > high_water:
                    high_water = page_high

            page += 1
            frappe.db.commit()

        # Advance cursor only after full success
        if high_water and high_water != settings.last_synced_at:
            frappe.db.set_single_value("Champions Hub Settings", "last_synced_at", high_water)
            frappe.db.commit()

    except requests.exceptions.HTTPError as e:
        frappe.log_error(
            f"Champions Hub: HTTP error {e.response.status_code} - {e.response.text[:500]}",
            "Champions Hub Sync",
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Champions Hub Sync")
