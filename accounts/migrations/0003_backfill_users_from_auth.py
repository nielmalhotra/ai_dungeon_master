from django.db import migrations


def backfill_users(apps, schema_editor):
    AuthUser = apps.get_model("auth", "User")
    AppUser = apps.get_model("accounts", "User")

    for auth_user in AuthUser.objects.exclude(email=""):
        AppUser.objects.get_or_create(email=auth_user.email)


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("accounts", "0002_repair_users_primary_key"),
    ]

    operations = [
        migrations.RunPython(backfill_users, reverse_code=migrations.RunPython.noop),
    ]
