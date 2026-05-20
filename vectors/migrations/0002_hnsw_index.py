from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("vectors", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                "CREATE INDEX IF NOT EXISTS reportvector_embedding_hnsw_idx "
                "ON vectors_reportvector USING hnsw (embedding vector_l2_ops);"
            ),
            reverse_sql="DROP INDEX IF EXISTS reportvector_embedding_hnsw_idx;",
        ),
    ]
