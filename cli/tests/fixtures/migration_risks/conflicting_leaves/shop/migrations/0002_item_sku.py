from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("shop", "0001_initial")]
    operations = [
        migrations.AddField(
            model_name="item",
            name="sku",
            field=models.CharField(default="", max_length=20),
        ),
    ]
