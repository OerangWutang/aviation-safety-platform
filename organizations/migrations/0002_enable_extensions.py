from django.db import migrations


def enable_extensions(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cursor.execute("CREATE EXTENSION IF NOT EXISTS ltree;")
        cursor.execute("CREATE EXTENSION IF NOT EXISTS postgis;")


class Migration(migrations.Migration):
    dependencies = [
        ("organizations", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(enable_extensions, migrations.RunPython.noop),
    ]
