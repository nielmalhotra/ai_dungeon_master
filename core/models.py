from django.conf import settings
from django.db import models
from django.db.models import Q
from pgvector.django import VectorField

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


class WorldLoreChunkTemplate(models.Model):
    scenario_key = models.CharField(max_length=64)
    version = models.CharField(max_length=32)
    active = models.BooleanField(default=True)
    source_file = models.CharField(max_length=128)
    section = models.CharField(max_length=200)
    chunk_number = models.PositiveIntegerField()
    content = models.TextField()
    metadata = models.JSONField(default=dict)
    embedding = VectorField(dimensions=settings.EMBEDDING_DIMENSIONS)

    class Meta:
        db_table = "world_lore_chunk_template"
        ordering = ["source_file", "chunk_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["scenario_key", "version", "source_file", "chunk_number"],
                name="unique_world_lore_chunk_template",
            )
        ]
        indexes = [
            models.Index(
                fields=["scenario_key", "active"],
                name="lore_template_active_idx",
            )
        ]

    def __str__(self):
        return f"{self.scenario_key} {self.version}: {self.section}"


class WorldLore(models.Model):
    dnd_session = models.ForeignKey(
        DndSession,
        on_delete=models.CASCADE,
        related_name="world_lore",
    )
    version = models.CharField(max_length=32)
    source_file = models.CharField(max_length=128)
    section = models.CharField(max_length=200)
    chunk_number = models.PositiveIntegerField()
    content = models.TextField()
    metadata = models.JSONField(default=dict)
    embedding = VectorField(dimensions=settings.EMBEDDING_DIMENSIONS)

    class Meta:
        db_table = "world_lore"
        ordering = ["source_file", "chunk_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["dnd_session", "source_file", "chunk_number"],
                name="unique_world_lore_chunk_per_session",
            )
        ]

    def __str__(self):
        return f"{self.source_file}: {self.section} ({self.version})"
