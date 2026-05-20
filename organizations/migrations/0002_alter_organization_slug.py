from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [
        ('organizations', '0001_initial'),
    ]
    operations = [
        migrations.AlterField(
            model_name='organization',
            name='slug',
            field=models.SlugField(max_length=120, unique=True),
        ),
    ]
