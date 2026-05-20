from django.http import JsonResponse
from django.db import connection


def health_check(request):
    """Simple health check endpoint."""
    try:
        connection.ensure_connection()
        db_status = 'ok'
    except Exception:
        db_status = 'error'

    return JsonResponse({
        'status': 'ok',
        'database': db_status,
    })
