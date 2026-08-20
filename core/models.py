from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from pgvector.django import VectorField

from accounts.models import User


def empty_visibility_state():
    return {"public_info": {}, "dm_only": {}}


def empty_initially_known_entities():
    return {
        "locations": [],
        "npcs": [],
        "quests": [],
        "world_lore": [],
    }


def empty_roll_modifiers():
    return []


def empty_npc_mechanics():
    return {
        "attributes": {
            "strength": 0,
            "dexterity": 0,
            "constitution": 0,
            "intelligence": 0,
            "wisdom": 0,
            "charisma": 0,
        },
        "trained_skills": [],
        "strong_saves": [],
    }


class Visibility(models.TextChoices):
    PUBLIC_INFO = "public_info", "Public info"
    DM_ONLY = "dm_only", "DM only"


class CharacterTemplate(models.Model):
    template_key = models.CharField(max_length=32, unique=True)
    character_template = models.JSONField()

    class Meta:
        db_table = "character_templates"
        ordering = ["template_key"]

    def __str__(self):
        return self.character_template.get("class", self.template_key)


class AbilityTemplate(models.Model):
    class Category(models.TextChoices):
        NON_COMBAT = "non_combat", "Non-combat"
        COMBAT = "combat", "Combat"

    character_template = models.ForeignKey(
        CharacterTemplate,
        on_delete=models.CASCADE,
        related_name="ability_templates",
    )
    ability_key = models.CharField(max_length=64)
    name = models.CharField(max_length=120)
    active = models.BooleanField(default=True)
    category = models.CharField(
        max_length=16,
        choices=Category.choices,
        default=Category.COMBAT,
    )
    description = models.TextField()
    resolution_json = models.JSONField(default=dict)
    effect_json = models.JSONField(default=dict)
    max_uses = models.PositiveIntegerField(null=True, blank=True)
    recharge = models.CharField(max_length=32, null=True, blank=True)
    embedding = VectorField(
        dimensions=settings.EMBEDDING_DIMENSIONS,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ability_template"
        ordering = ["character_template", "ability_key"]
        constraints = [
            models.UniqueConstraint(
                fields=["character_template", "ability_key"],
                name="uniq_character_ability_tpl",
            )
        ]

    def __str__(self):
        return f"{self.character_template}: {self.name}"


class ItemTemplate(models.Model):
    template_key = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=120)
    active = models.BooleanField(default=True)
    definition_json = models.JSONField(default=empty_visibility_state)
    mechanics_json = models.JSONField(default=dict)
    public_embedding = VectorField(
        dimensions=settings.EMBEDDING_DIMENSIONS,
        null=True,
        blank=True,
    )
    dm_embedding = VectorField(
        dimensions=settings.EMBEDDING_DIMENSIONS,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "item_template"
        ordering = ["template_key"]

    def __str__(self):
        return self.name


class CharacterTemplateItem(models.Model):
    character_template = models.ForeignKey(
        CharacterTemplate,
        on_delete=models.CASCADE,
        related_name="starting_items",
    )
    item_template = models.ForeignKey(
        ItemTemplate,
        on_delete=models.CASCADE,
        related_name="character_loadouts",
    )
    starting_quantity = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "character_template_item"
        ordering = ["character_template", "item_template"]
        constraints = [
            models.UniqueConstraint(
                fields=["character_template", "item_template"],
                name="uniq_character_starting_item",
            )
        ]


class DndSession(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        COMPLETED = "completed", "Completed"
        ABANDONED = "abandoned", "Abandoned"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="dnd_sessions")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    scenario_key = models.CharField(max_length=64, default=settings.SCENARIO_KEY)
    scenario_version = models.PositiveIntegerField(default=1)
    turn_number = models.PositiveBigIntegerField(default=0)
    opening_text = models.TextField(default="")
    initially_known_entities_json = models.JSONField(
        default=empty_initially_known_entities
    )
    current_location = models.ForeignKey(
        "LocationInstance",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="focused_sessions",
    )
    main_quest = models.ForeignKey(
        "QuestInstance",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="main_for_sessions",
    )
    state_json = models.JSONField(default=empty_visibility_state)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "dnd_session"
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(status="active"),
                name="one_active_dnd_session_per_user",
            )
        ]
        indexes = [
            models.Index(
                fields=["scenario_key", "scenario_version"],
                name="session_scenario_idx",
            )
        ]

    def __str__(self):
        return f"{self.user.email} ({self.status})"


class CharacterInstance(models.Model):
    dnd_session = models.ForeignKey(
        DndSession,
        on_delete=models.CASCADE,
        related_name="characters",
    )
    name = models.CharField(max_length=80)
    template_json = models.JSONField()
    mechanics_json = models.JSONField(default=dict)
    modifiers_json = models.JSONField(default=empty_roll_modifiers)
    current_location = models.ForeignKey(
        "LocationInstance",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="characters",
    )
    state_json = models.JSONField(default=empty_visibility_state)
    relationships_json = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "character_instance"
        ordering = ["id"]

    def __str__(self):
        character_class = self.template_json.get("class", "Character")
        return f"{self.name} ({character_class})"


class AbilityInstance(models.Model):
    character = models.ForeignKey(
        CharacterInstance,
        on_delete=models.CASCADE,
        related_name="abilities",
    )
    template = models.ForeignKey(
        AbilityTemplate,
        on_delete=models.PROTECT,
        related_name="instances",
    )
    remaining_uses = models.PositiveIntegerField(null=True, blank=True)
    state_json = models.JSONField(default=empty_visibility_state)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ability_instance"
        ordering = ["character", "template"]
        constraints = [
            models.UniqueConstraint(
                fields=["character", "template"],
                name="uniq_character_ability_instance",
            )
        ]

    def __str__(self):
        return f"{self.character.name}: {self.template.name}"


class ItemInstance(models.Model):
    class OwnerType(models.TextChoices):
        CHARACTER = "character", "Character"
        NPC = "npc", "NPC"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        CONSUMED = "consumed", "Consumed"
        DESTROYED = "destroyed", "Destroyed"

    dnd_session = models.ForeignKey(
        DndSession,
        on_delete=models.CASCADE,
        related_name="items",
    )
    template = models.ForeignKey(
        ItemTemplate,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="instances",
    )
    name = models.CharField(max_length=120)
    owner_type = models.CharField(
        max_length=16,
        choices=OwnerType.choices,
        null=True,
        blank=True,
    )
    owner_id = models.PositiveBigIntegerField(null=True, blank=True)
    current_location = models.ForeignKey(
        "LocationInstance",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="items",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    state_json = models.JSONField(default=empty_visibility_state)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "item_instance"
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                check=(
                    Q(owner_type__isnull=True, owner_id__isnull=True)
                    | Q(owner_type__isnull=False, owner_id__isnull=False)
                ),
                name="item_owner_pair_ck",
            ),
            models.CheckConstraint(
                check=(
                    Q(owner_id__isnull=True)
                    | Q(current_location__isnull=True)
                ),
                name="item_owner_location_exclusive_ck",
            ),
            models.CheckConstraint(
                check=(
                    ~Q(status="active")
                    | Q(owner_id__isnull=False)
                    | Q(current_location__isnull=False)
                ),
                name="item_active_placement_ck",
            ),
        ]
        indexes = [
            models.Index(
                fields=["dnd_session", "owner_type", "owner_id", "status"],
                name="item_session_owner_idx",
            ),
            models.Index(
                fields=["dnd_session", "current_location", "status"],
                name="item_session_loc_idx",
            ),
        ]

    def __str__(self):
        return self.name


class LocationTemplate(models.Model):
    class InitialStatus(models.TextChoices):
        HIDDEN = "hidden", "Hidden"
        ACTIVE = "active", "Active"
        DESTROYED = "destroyed", "Destroyed"

    definition_uuid = models.UUIDField()
    scenario_key = models.CharField(max_length=64)
    version = models.PositiveIntegerField()
    active = models.BooleanField(default=True)
    source_file = models.CharField(max_length=255)
    name = models.CharField(max_length=160)
    parent_template = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="child_templates",
    )
    initial_status = models.CharField(
        max_length=16,
        choices=InitialStatus.choices,
        default=InitialStatus.ACTIVE,
    )
    definition_json = models.JSONField(default=empty_visibility_state)
    relationships_json = models.JSONField(default=list)
    metadata_json = models.JSONField(default=dict)
    public_embedding = VectorField(
        dimensions=settings.EMBEDDING_DIMENSIONS,
        null=True,
        blank=True,
    )
    dm_embedding = VectorField(
        dimensions=settings.EMBEDDING_DIMENSIONS,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "location_template"
        ordering = ["scenario_key", "version", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["scenario_key", "version", "definition_uuid"],
                name="uniq_location_tpl_uuid",
            ),
        ]
        indexes = [
            models.Index(
                fields=["scenario_key", "active"],
                name="location_tpl_active_idx",
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.scenario_key} v{self.version})"


class LocationInstance(models.Model):
    class Status(models.TextChoices):
        HIDDEN = "hidden", "Hidden"
        ACTIVE = "active", "Active"
        DESTROYED = "destroyed", "Destroyed"

    dnd_session = models.ForeignKey(
        DndSession,
        on_delete=models.CASCADE,
        related_name="locations",
    )
    template = models.ForeignKey(
        LocationTemplate,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="instances",
    )
    name = models.CharField(max_length=160)
    parent_location = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="child_locations",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    state_json = models.JSONField(default=empty_visibility_state)
    relationships_json = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "location_instance"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["dnd_session", "template"],
                name="uniq_location_instance_tpl",
            )
        ]
        indexes = [
            models.Index(
                fields=["dnd_session", "status"],
                name="location_session_status_idx",
            )
        ]

    def __str__(self):
        return self.name


class NPCTemplate(models.Model):
    class InitialStatus(models.TextChoices):
        HIDDEN = "hidden", "Hidden"
        ACTIVE = "active", "Active"
        DEAD = "dead", "Dead"

    definition_uuid = models.UUIDField()
    scenario_key = models.CharField(max_length=64)
    version = models.PositiveIntegerField()
    active = models.BooleanField(default=True)
    source_file = models.CharField(max_length=255)
    name = models.CharField(max_length=120)
    initial_location_template = models.ForeignKey(
        LocationTemplate,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="initial_npc_templates",
    )
    initial_status = models.CharField(
        max_length=16,
        choices=InitialStatus.choices,
        default=InitialStatus.ACTIVE,
    )
    definition_json = models.JSONField(default=empty_visibility_state)
    mechanics_json = models.JSONField(default=empty_npc_mechanics)
    relationships_json = models.JSONField(default=list)
    metadata_json = models.JSONField(default=dict)
    public_embedding = VectorField(
        dimensions=settings.EMBEDDING_DIMENSIONS,
        null=True,
        blank=True,
    )
    dm_embedding = VectorField(
        dimensions=settings.EMBEDDING_DIMENSIONS,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "npc_template"
        ordering = ["scenario_key", "version", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["scenario_key", "version", "definition_uuid"],
                name="uniq_npc_tpl_uuid",
            )
        ]
        indexes = [
            models.Index(
                fields=["scenario_key", "active"],
                name="npc_tpl_active_idx",
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.scenario_key} v{self.version})"


class NPCInstance(models.Model):
    class Status(models.TextChoices):
        HIDDEN = "hidden", "Hidden"
        ACTIVE = "active", "Active"
        DEAD = "dead", "Dead"

    dnd_session = models.ForeignKey(
        DndSession,
        on_delete=models.CASCADE,
        related_name="npcs",
    )
    template = models.ForeignKey(
        NPCTemplate,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="instances",
    )
    name = models.CharField(max_length=120)
    current_location = models.ForeignKey(
        LocationInstance,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="npcs",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    mechanics_json = models.JSONField(default=empty_npc_mechanics)
    modifiers_json = models.JSONField(default=empty_roll_modifiers)
    state_json = models.JSONField(default=empty_visibility_state)
    relationships_json = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "npc_instance"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["dnd_session", "template"],
                name="uniq_npc_instance_tpl",
            )
        ]
        indexes = [
            models.Index(
                fields=["dnd_session", "current_location", "status"],
                name="npc_session_loc_status_idx",
            )
        ]

    def __str__(self):
        return self.name


class QuestTemplate(models.Model):
    class InitialStatus(models.TextChoices):
        HIDDEN = "hidden", "Hidden"
        AVAILABLE = "available", "Available"
        ACTIVE = "active", "Active"
        FINISHED = "finished", "Finished"

    definition_uuid = models.UUIDField()
    scenario_key = models.CharField(max_length=64)
    version = models.PositiveIntegerField()
    active = models.BooleanField(default=True)
    source_file = models.CharField(max_length=255)
    title = models.CharField(max_length=200)
    initial_status = models.CharField(
        max_length=16,
        choices=InitialStatus.choices,
        default=InitialStatus.HIDDEN,
    )
    definition_json = models.JSONField(default=empty_visibility_state)
    relationships_json = models.JSONField(default=list)
    metadata_json = models.JSONField(default=dict)
    public_embedding = VectorField(
        dimensions=settings.EMBEDDING_DIMENSIONS,
        null=True,
        blank=True,
    )
    dm_embedding = VectorField(
        dimensions=settings.EMBEDDING_DIMENSIONS,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "quest_template"
        ordering = ["scenario_key", "version", "title"]
        constraints = [
            models.UniqueConstraint(
                fields=["scenario_key", "version", "definition_uuid"],
                name="uniq_quest_tpl_uuid",
            )
        ]
        indexes = [
            models.Index(
                fields=["scenario_key", "active"],
                name="quest_tpl_active_idx",
            )
        ]

    def __str__(self):
        return f"{self.title} ({self.scenario_key} v{self.version})"


class QuestInstance(models.Model):
    class Status(models.TextChoices):
        HIDDEN = "hidden", "Hidden"
        AVAILABLE = "available", "Available"
        ACTIVE = "active", "Active"
        FINISHED = "finished", "Finished"

    dnd_session = models.ForeignKey(
        DndSession,
        on_delete=models.CASCADE,
        related_name="quests",
    )
    template = models.ForeignKey(
        QuestTemplate,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="instances",
    )
    title = models.CharField(max_length=200)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.HIDDEN,
    )
    current_stage = models.PositiveIntegerField(default=0)
    relationships_json = models.JSONField(default=list)
    state_json = models.JSONField(default=empty_visibility_state)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "quest_instance"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["dnd_session", "template"],
                name="uniq_quest_instance_tpl",
            )
        ]
        indexes = [
            models.Index(
                fields=["dnd_session", "status"],
                name="quest_session_status_idx",
            )
        ]

    def __str__(self):
        return self.title


class WorldLoreChunkTemplate(models.Model):
    definition_uuid = models.UUIDField()
    scenario_key = models.CharField(max_length=64)
    version = models.PositiveIntegerField()
    active = models.BooleanField(default=True)
    source_file = models.CharField(max_length=255)
    title = models.CharField(max_length=200)
    section = models.CharField(max_length=200)
    chunk_number = models.PositiveIntegerField()
    visibility = models.CharField(max_length=16, choices=Visibility.choices)
    content = models.TextField()
    relationships_json = models.JSONField(default=list)
    metadata_json = models.JSONField(default=dict)
    embedding = VectorField(dimensions=settings.EMBEDDING_DIMENSIONS)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "world_lore_chunk_template"
        ordering = ["source_file", "chunk_number"]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "scenario_key",
                    "version",
                    "definition_uuid",
                    "chunk_number",
                ],
                name="uniq_lore_tpl_chunk",
            ),
        ]
        indexes = [
            models.Index(
                fields=["scenario_key", "active", "visibility"],
                name="lore_tpl_active_vis_idx",
            )
        ]

    def __str__(self):
        return f"{self.scenario_key} v{self.version}: {self.section}"


class WorldLore(models.Model):
    dnd_session = models.ForeignKey(
        DndSession,
        on_delete=models.CASCADE,
        related_name="world_lore",
    )
    template = models.ForeignKey(
        WorldLoreChunkTemplate,
        on_delete=models.PROTECT,
        related_name="campaign_lore",
    )
    relationships_json = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "world_lore"
        ordering = ["template__source_file", "template__chunk_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["dnd_session", "template"],
                name="uniq_session_lore_tpl",
            )
        ]

    def __str__(self):
        return f"{self.template.source_file}: {self.template.section}"


class CampaignTurn(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    dnd_session = models.ForeignKey(
        DndSession,
        on_delete=models.CASCADE,
        related_name="turns",
    )
    turn_number = models.PositiveBigIntegerField()
    player_input = models.TextField()
    dm_response_json = models.JSONField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "campaign_turn"
        ordering = ["dnd_session", "turn_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["dnd_session", "turn_number"],
                name="uniq_campaign_turn_number",
            )
        ]


class AgentRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    campaign_turn = models.ForeignKey(
        CampaignTurn,
        on_delete=models.CASCADE,
        related_name="agent_runs",
    )
    run_number = models.PositiveIntegerField(default=1)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.RUNNING,
    )
    model = models.CharField(max_length=100)
    model_settings_json = models.JSONField(default=dict)
    input_json = models.JSONField(default=dict)
    output_json = models.JSONField(null=True, blank=True)
    trace_json = models.JSONField(default=dict)
    provider_trace_id = models.CharField(max_length=255, null=True, blank=True)
    retry_count = models.PositiveIntegerField(default=0)
    validation_failure_count = models.PositiveIntegerField(default=0)
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    estimated_cost_usd = models.DecimalField(
        max_digits=12,
        decimal_places=6,
        null=True,
        blank=True,
    )
    error_json = models.JSONField(null=True, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = "agent_run"
        ordering = ["campaign_turn", "run_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign_turn", "run_number"],
                name="uniq_turn_agent_run",
            )
        ]


class RetrievedContextRecord(models.Model):
    class SourceType(models.TextChoices):
        WORLD_LORE = "world_lore", "World lore"
        NPC = "npc", "NPC"
        LOCATION = "location", "Location"
        QUEST = "quest", "Quest"
        WORLD_EVENT = "world_event", "World event"
        ABILITY = "ability", "Ability"
        ITEM = "item", "Item"

    agent_run = models.ForeignKey(
        AgentRun,
        on_delete=models.CASCADE,
        related_name="retrieved_context",
    )
    query = models.TextField()
    source_type = models.CharField(max_length=32, choices=SourceType.choices)
    source_id = models.BigIntegerField()
    visibility = models.CharField(max_length=16, choices=Visibility.choices)
    content_snapshot = models.TextField()
    similarity_score = models.FloatField(null=True, blank=True)
    initial_rank = models.PositiveIntegerField(null=True, blank=True)
    rerank_score = models.FloatField(null=True, blank=True)
    final_rank = models.PositiveIntegerField(null=True, blank=True)
    included_in_prompt = models.BooleanField(default=False)
    exclusion_reason = models.CharField(max_length=100, null=True, blank=True)
    prompt_position = models.PositiveIntegerField(null=True, blank=True)
    token_count = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "retrieved_context_record"
        ordering = ["agent_run", "initial_rank", "id"]
        constraints = [
            models.CheckConstraint(
                check=Q(source_id__gt=0),
                name="retrieved_source_id_ck",
            )
        ]
        indexes = [
            models.Index(
                fields=["agent_run", "included_in_prompt"],
                name="retrieved_run_included_idx",
            ),
            models.Index(
                fields=["source_type", "source_id"],
                name="retrieved_source_idx",
            ),
        ]


class ToolCallRecord(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    agent_run = models.ForeignKey(
        AgentRun,
        on_delete=models.CASCADE,
        related_name="tool_calls",
    )
    call_id = models.CharField(max_length=255)
    sequence_number = models.PositiveIntegerField()
    tool_name = models.CharField(max_length=100)
    arguments_json = models.JSONField(default=dict)
    result_json = models.JSONField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.RUNNING,
    )
    error_json = models.JSONField(null=True, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = "tool_call_record"
        ordering = ["agent_run", "sequence_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["agent_run", "call_id"],
                name="uniq_run_tool_call_id",
            ),
            models.UniqueConstraint(
                fields=["agent_run", "sequence_number"],
                name="uniq_run_tool_sequence",
            ),
        ]


class WorldEvent(models.Model):
    campaign_turn = models.ForeignKey(
        CampaignTurn,
        on_delete=models.CASCADE,
        related_name="world_events",
    )
    tool_call = models.ForeignKey(
        ToolCallRecord,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="world_events",
    )
    sequence_number = models.PositiveIntegerField()
    event_type = models.CharField(max_length=64)
    related_entities_json = models.JSONField(default=list)
    state_json = models.JSONField(default=empty_visibility_state)
    importance = models.PositiveSmallIntegerField(default=3)
    public_embedding = VectorField(
        dimensions=settings.EMBEDDING_DIMENSIONS,
        null=True,
        blank=True,
    )
    dm_embedding = VectorField(
        dimensions=settings.EMBEDDING_DIMENSIONS,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "world_event"
        ordering = ["campaign_turn", "sequence_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign_turn", "sequence_number"],
                name="uniq_turn_event_sequence",
            ),
            models.CheckConstraint(
                check=Q(importance__gte=1) & Q(importance__lte=5),
                name="world_event_importance_ck",
            ),
        ]
        indexes = [
            models.Index(
                fields=["event_type", "created_at"],
                name="world_event_type_time_idx",
            )
        ]
