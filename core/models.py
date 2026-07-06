from django.db import models
from django.db.models import Q

from accounts.models import User


class CharacterTemplate(models.Model):
    template_key = models.CharField(max_length=32, unique=True)
    character_template = models.JSONField()

    class Meta:
        db_table = "character_templates"
        ordering = ["template_key"]

    def __str__(self):
        return self.character_template.get("class", self.template_key)


class DndSession(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="dnd_sessions")
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "dnd_session"
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(active=True),
                name="one_active_dnd_session_per_user",
            )
        ]

    def __str__(self):
        return f"{self.user.email} ({'active' if self.active else 'inactive'})"


class CharacterInstance(models.Model):
    dnd_session = models.ForeignKey(
        DndSession,
        on_delete=models.CASCADE,
        related_name="characters",
    )
    name = models.CharField(max_length=80)
    template_json = models.JSONField()

    class Meta:
        db_table = "character_instance"
        ordering = ["id"]

    def __str__(self):
        character_class = self.template_json.get("class", "Character")
        return f"{self.name} ({character_class})"
