import frappe
import requests
from champions_hub_integ.sync.enrollments import sync_enrollments


@frappe.whitelist()
def test_connection():
    """Test the API connection by fetching page 1 with per_page=1."""
    frappe.only_for("System Manager")
    settings = frappe.get_single("Champions Hub Settings")
    token = settings.get_password("api_token")
    if not token:
        return {"success": False, "error": "API token is not configured"}

    base_url = (settings.base_url or "https://champions-hub.com/api/v1").rstrip("/")
    try:
        resp = requests.get(
            f"{base_url}/integrations/enrollments",
            headers={"Authorization": f"Bearer {token}"},
            params={"per_page": 1, "page": 1},
            timeout=15,
        )
        if resp.status_code == 401:
            return {"success": False, "error": "Authentication failed (401). Check your API token."}
        if resp.status_code == 503:
            return {"success": False, "error": "Integration is disabled server-side (503). Contact Champions Hub."}
        resp.raise_for_status()
        meta = resp.json().get("meta", {})
        return {"success": True, "total": meta.get("total", 0)}
    except requests.exceptions.ConnectionError:
        return {"success": False, "error": f"Cannot reach {base_url}. Check the Base URL."}
    except requests.exceptions.Timeout:
        return {"success": False, "error": "Request timed out."}
    except Exception as e:
        return {"success": False, "error": str(e)}


@frappe.whitelist()
def trigger_sync():
    """Manually trigger a Champions Hub enrollment sync."""
    frappe.only_for("System Manager")
    frappe.enqueue(sync_enrollments, queue="long", timeout=3600)
    return {"message": "Sync job enqueued"}


@frappe.whitelist()
def sync_status():
    """Return the current sync status."""
    frappe.only_for("System Manager")
    settings = frappe.get_single("Champions Hub Settings")
    total_logs = frappe.db.count("Champions Enrollment Log")
    success_logs = frappe.db.count("Champions Enrollment Log", {"sync_status": "Success"})
    error_logs = frappe.db.count("Champions Enrollment Log", {"sync_status": "Error"})

    return {
        "enabled": bool(settings.enabled),
        "last_synced_at": settings.last_synced_at,
        "total_enrollments": total_logs,
        "success": success_logs,
        "errors": error_logs,
    }
