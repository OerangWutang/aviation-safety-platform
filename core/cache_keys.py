def reports_cache_key(org_id, scope, filters, view_version="v1"):
    return f"reports:{org_id}:{scope}:{filters}:{view_version}"
