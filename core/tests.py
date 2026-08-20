from copy import deepcopy
from io import StringIO
from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from accounts.models import User as AppUser

from .campaigns import finish_quest
from .character_templates import (
    load_character_templates,
    load_item_templates,
    sync_character_templates,
    sync_gameplay_template_embeddings,
)
from .game_tools import execute_game_tool
from .models import (
    AbilityInstance,
    AbilityTemplate,
    AgentRun,
    CampaignTurn,
    CharacterInstance,
    CharacterTemplate,
    DndSession,
    ItemInstance,
    ItemTemplate,
    LocationInstance,
    LocationTemplate,
    NPCTemplate,
    QuestInstance,
    QuestTemplate,
    Visibility,
    WorldLoreChunkTemplate,
)
from .scenario_lore import (
    ScenarioLoreError,
    SourceRelationship,
    UUID_PLACEHOLDER,
    build_scenario_embedding_inputs,
    create_definition_file,
    create_init_file,
    embed_scenario_inputs,
    grouped_known_entities,
    load_scenario_release,
    parse_definition_file,
    populate_scenario_uuids,
    render_definition_file,
    sync_scenario_templates,
)


ZERO_EMBEDDING = [0.0] * 3072
LOCATION_UUID = UUID("94546ab4-3d58-40e2-af49-70753e784f25")
NPC_UUID = UUID("db1041ba-2f74-44db-ba91-ef7408afff1a")
QUEST_UUID = UUID("4b802d31-63b9-4467-9710-b7b5b2c0b793")
LORE_UUID = UUID("d5218545-8822-4b1e-8b76-d5196cc584dc")


def embedded_fixture(inputs, **kwargs):
    return {item.key: ZERO_EMBEDDING for item in inputs}


def synchronize_fixture():
    with patch(
        "core.scenario_lore.embed_scenario_inputs",
        side_effect=embedded_fixture,
    ):
        return sync_scenario_templates()


def copy_whitesparrow_release(root, version):
    source = Path(settings.SCENARIO_DIR) / "whitesparrow" / "v1"
    target = Path(root) / "whitesparrow" / f"v{version}"
    copytree(source, target)
    return target


def replace_uuid_with_placeholder(path):
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    lines[0] = f"UUID: {UUID_PLACEHOLDER}"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class ScenarioSourceTests(TestCase):
    def test_validation_command_accepts_scenario_version(self):
        output = StringIO()

        call_command(
            "validate_scenario",
            scenario_version=1,
            stdout=output,
        )

        self.assertIn(
            "Validated 4 definitions for whitesparrow v1",
            output.getvalue(),
        )

    def test_release_parses_init_entities_statuses_and_relationships(self):
        release = load_scenario_release()

        self.assertEqual(release.scenario_key, "whitesparrow")
        self.assertEqual(release.version, 1)
        self.assertEqual(len(release.definitions), 4)
        self.assertEqual(len(release.lore_chunks), 2)
        self.assertEqual(
            {definition.definition_type for definition in release.definitions},
            {"location", "npc", "quest", "world_lore"},
        )
        self.assertEqual(
            release.initialization.starting_location_uuid,
            LOCATION_UUID,
        )
        self.assertEqual(release.initialization.main_quest_uuid, QUEST_UUID)
        self.assertIn("Light rain falls", release.initialization.opening)
        self.assertIn("no further actions", release.initialization.dm_only)
        self.assertEqual(
            grouped_known_entities(release),
            {
                "locations": [str(LOCATION_UUID)],
                "npcs": [],
                "quests": [str(QUEST_UUID)],
                "world_lore": [str(LORE_UUID)],
            },
        )

        npc = release.definitions_of_type("npc")[0]
        quest = release.definitions_of_type("quest")[0]
        self.assertEqual(npc.initial_status, "active")
        self.assertEqual(
            npc.relationships,
            (SourceRelationship("located_in", "location", LOCATION_UUID),),
        )
        self.assertEqual(quest.initial_status, "available")
        self.assertEqual(
            quest.relationships,
            (
                SourceRelationship("involves", "npc", NPC_UUID),
                SourceRelationship("involves", "location", LOCATION_UUID),
            ),
        )
        self.assertEqual(
            {chunk.visibility for chunk in release.lore_chunks},
            {Visibility.PUBLIC_INFO, Visibility.DM_ONLY},
        )

    def test_definition_requires_uuid_on_first_line(self):
        with TemporaryDirectory() as directory:
            release_dir = Path(directory) / "v1"
            location_dir = release_dir / "locations"
            location_dir.mkdir(parents=True)
            path = location_dir / "bad.txt"
            path.write_text(
                "NAME: Bad\nINITIAL STATUS: active\n\nRELATIONSHIPS:\n\n"
                "PUBLIC:\n\nSafe.\n\nDM_ONLY:\n\nHidden.\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ScenarioLoreError, "must begin with"):
                parse_definition_file(path)

    def test_definition_and_init_creation_functions_write_contract(self):
        with TemporaryDirectory() as directory:
            release_dir = Path(directory) / "v2"
            target = create_definition_file(
                release_dir=release_dir,
                definition_type="npc",
                filename="new_npc.txt",
                name="New NPC",
                initial_status="hidden",
                relationships=(
                    SourceRelationship("located_in", "location", LOCATION_UUID),
                ),
                public_info="Safe description.",
                dm_only="Hidden description.",
            )
            initialization = create_init_file(
                release_dir=release_dir,
                starting_location_uuid=LOCATION_UUID,
                main_quest_uuid=QUEST_UUID,
                opening="The adventure opens.",
                known_entity_uuids=(LOCATION_UUID,),
                dm_only="Private setup.",
            )

            self.assertTrue(
                target.read_text(encoding="utf-8").startswith(
                    f"UUID: {UUID_PLACEHOLDER}\nNAME: New NPC"
                )
            )
            parsed = parse_definition_file(target, allow_placeholder=True)
            self.assertEqual(parsed.name, "New NPC")
            self.assertEqual(parsed.initial_status, "hidden")
            self.assertEqual(initialization.name, "init.txt")
            self.assertTrue(
                initialization.read_text(encoding="utf-8").startswith(
                    "STARTING LOCATION:"
                )
            )

    def test_relationship_must_reference_correct_entity_type(self):
        with TemporaryDirectory() as directory:
            release_dir = copy_whitesparrow_release(directory, 1)
            quest_path = release_dir / "quests" / "investigate_the_night_blades.txt"
            text = quest_path.read_text(encoding="utf-8").replace(
                f"involves | npc | {NPC_UUID}",
                f"involves | location | {NPC_UUID}",
            )
            quest_path.write_text(text, encoding="utf-8")

            with self.assertRaisesRegex(ScenarioLoreError, "unknown location"):
                load_scenario_release(scenario_dir=directory)

    def test_embedding_requests_are_batched_and_validated(self):
        inputs = build_scenario_embedding_inputs(load_scenario_release())

        def embedding_response(**request):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(index=index, embedding=[float(index)] * 3072)
                    for index in range(len(request["input"]))
                ]
            )

        create_embedding = Mock(side_effect=embedding_response)
        client = SimpleNamespace(
            embeddings=SimpleNamespace(create=create_embedding)
        )

        embedded = embed_scenario_inputs(inputs, client=client, batch_size=3)

        self.assertEqual(create_embedding.call_count, 3)
        request = create_embedding.call_args_list[0].kwargs
        self.assertEqual(request["model"], "text-embedding-3-large")
        self.assertEqual(request["dimensions"], 3072)
        self.assertEqual(len(request["input"]), 3)
        self.assertEqual(len(embedded), len(inputs))


class ScenarioUUIDTests(TestCase):
    def test_population_reuses_exact_matches_and_generates_new_uuid(self):
        with TemporaryDirectory() as directory:
            copy_whitesparrow_release(directory, 1)
            release_dir = copy_whitesparrow_release(directory, 2)
            definition_paths = sorted(
                path
                for folder in ("locations", "npcs", "quests", "worldlore")
                for path in (release_dir / folder).glob("*.txt")
            )
            for path in definition_paths:
                replace_uuid_with_placeholder(path)
            new_npc = release_dir / "npcs" / "traveler.txt"
            new_npc.write_text(
                render_definition_file(
                    definition_type="npc",
                    name="A Passing Traveler",
                    initial_status="hidden",
                    relationships=(
                        SourceRelationship(
                            "located_in",
                            "location",
                            LOCATION_UUID,
                        ),
                    ),
                    public_info="A road-worn traveler.",
                    dm_only="The traveler is avoiding the sheriff.",
                ),
                encoding="utf-8",
            )

            populated = populate_scenario_uuids(
                scenario_dir=directory,
                version=2,
            )

            self.assertEqual(len(populated), 5)
            expected = {
                "locations/whitesparrow_village.txt": LOCATION_UUID,
                "npcs/sheriff_ruth_willowmane.txt": NPC_UUID,
                "quests/investigate_the_night_blades.txt": QUEST_UUID,
                "worldlore/the_night_blades.txt": LORE_UUID,
            }
            for source_file, expected_uuid in expected.items():
                self.assertEqual(UUID(populated[source_file]), expected_uuid)
            generated_uuid = UUID(populated["npcs/traveler.txt"])
            self.assertNotIn(generated_uuid, expected.values())
            self.assertNotIn(UUID_PLACEHOLDER, new_npc.read_text(encoding="utf-8"))
            self.assertEqual(load_scenario_release(scenario_dir=directory).version, 2)

    def test_population_is_atomic_when_release_validation_fails(self):
        with TemporaryDirectory() as directory:
            copy_whitesparrow_release(directory, 1)
            release_dir = copy_whitesparrow_release(directory, 2)
            definition_paths = sorted(
                path
                for folder in ("locations", "npcs", "quests", "worldlore")
                for path in (release_dir / folder).glob("*.txt")
            )
            for path in definition_paths:
                replace_uuid_with_placeholder(path)
            npc_path = release_dir / "npcs" / "sheriff_ruth_willowmane.txt"
            npc_path.write_text(
                npc_path.read_text(encoding="utf-8").replace(
                    str(LOCATION_UUID),
                    str(uuid4()),
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ScenarioLoreError, "unknown location"):
                populate_scenario_uuids(scenario_dir=directory, version=2)

            for path in definition_paths:
                self.assertTrue(
                    path.read_text(encoding="utf-8").startswith(
                        f"UUID: {UUID_PLACEHOLDER}"
                    )
                )


class ScenarioSynchronizationTests(TestCase):
    def test_sync_builds_linked_immutable_active_templates(self):
        old_template = WorldLoreChunkTemplate.objects.create(
            definition_uuid=uuid4(),
            scenario_key="whitesparrow",
            version=2,
            active=True,
            source_file="worldlore/old.txt",
            title="Old Lore",
            section="Public",
            chunk_number=1,
            visibility=Visibility.PUBLIC_INFO,
            content="Old lore.",
            embedding=ZERO_EMBEDDING,
        )

        version, count = synchronize_fixture()

        self.assertEqual((version, count), (1, 5))
        self.assertEqual(LocationTemplate.objects.filter(active=True).count(), 1)
        self.assertEqual(NPCTemplate.objects.filter(active=True).count(), 1)
        self.assertEqual(QuestTemplate.objects.filter(active=True).count(), 1)
        self.assertEqual(
            WorldLoreChunkTemplate.objects.filter(active=True).count(),
            2,
        )
        old_template.refresh_from_db()
        self.assertFalse(old_template.active)

        location = LocationTemplate.objects.get(active=True)
        npc = NPCTemplate.objects.get(active=True)
        quest = QuestTemplate.objects.get(active=True)
        self.assertEqual(npc.initial_location_template, location)
        self.assertEqual(
            npc.relationships_json,
            [
                {
                    "relation": "located_in",
                    "target": {"type": "location", "id": location.id},
                }
            ],
        )
        self.assertEqual(
            quest.relationships_json,
            [
                {
                    "relation": "involves",
                    "target": {"type": "npc", "id": npc.id},
                },
                {
                    "relation": "involves",
                    "target": {"type": "location", "id": location.id},
                },
            ],
        )

        self.assertEqual(synchronize_fixture(), (1, 5))
        self.assertEqual(LocationTemplate.objects.count(), 1)
        self.assertEqual(NPCTemplate.objects.count(), 1)
        self.assertEqual(QuestTemplate.objects.count(), 1)
        self.assertEqual(WorldLoreChunkTemplate.objects.count(), 3)


class CampaignCreationTests(TestCase):
    def setUp(self):
        synchronize_fixture()
        self.auth_user = User.objects.create_user(
            username="player@example.com",
            email="player@example.com",
        )
        self.client.force_login(self.auth_user)

    def create_campaign_through_view(self):
        return self.client.post(
            reverse("home"),
            {
                "selected_templates": ["warrior", "druid", "bard"],
                "name_warrior": "Alden",
                "name_druid": "Briar",
                "name_bard": "Calla",
            },
        )

    def test_creation_instantiates_bootstrap_state_and_relationships(self):
        original_warrior = deepcopy(
            CharacterTemplate.objects.get(template_key="warrior").character_template
        )

        response = self.create_campaign_through_view()

        self.assertRedirects(response, reverse("home"))
        campaign = DndSession.objects.get(status=DndSession.Status.ACTIVE)
        self.assertEqual(campaign.scenario_version, 1)
        self.assertIn("Light rain falls", campaign.opening_text)
        self.assertEqual(
            campaign.initially_known_entities_json,
            {
                "locations": [str(LOCATION_UUID)],
                "npcs": [],
                "quests": [str(QUEST_UUID)],
                "world_lore": [str(LORE_UUID)],
            },
        )
        self.assertEqual(campaign.locations.count(), 1)
        self.assertEqual(campaign.npcs.count(), 1)
        self.assertEqual(campaign.quests.count(), 1)
        self.assertEqual(campaign.world_lore.count(), 2)
        self.assertEqual(campaign.characters.count(), 3)

        location = campaign.locations.get()
        npc = campaign.npcs.get()
        quest = campaign.quests.get()
        self.assertEqual(campaign.current_location, location)
        self.assertEqual(campaign.main_quest, quest)
        self.assertEqual(quest.status, QuestInstance.Status.ACTIVE)
        self.assertEqual(npc.current_location, location)
        self.assertTrue(npc.state_json["public_info"])
        self.assertTrue(npc.state_json["dm_only"])
        self.assertEqual(
            npc.relationships_json,
            [
                {
                    "relation": "located_in",
                    "target": {"type": "location", "id": location.id},
                }
            ],
        )
        self.assertEqual(
            quest.relationships_json,
            [
                {
                    "relation": "involves",
                    "target": {"type": "npc", "id": npc.id},
                },
                {
                    "relation": "involves",
                    "target": {"type": "location", "id": location.id},
                },
            ],
        )
        warrior = campaign.characters.get(name="Alden")
        self.assertEqual(warrior.template_json, original_warrior)
        for field in (
            "hp",
            "ac",
            "initiative_modifier",
            "attributes",
            "trained_skills",
            "strong_saves",
        ):
            self.assertEqual(warrior.mechanics_json[field], original_warrior[field])
        self.assertEqual(warrior.current_location, location)
        self.assertEqual(
            set(warrior.abilities.values_list("template__ability_key", flat=True)),
            {ability["key"] for ability in original_warrior["abilities"]},
        )
        for item in original_warrior["gear"]:
            self.assertEqual(
                campaign.items.filter(
                    owner_type=ItemInstance.OwnerType.CHARACTER,
                    owner_id=warrior.id,
                    template__template_key=item["key"],
                    status=ItemInstance.Status.ACTIVE,
                ).count(),
                item["quantity"],
            )

    def test_opening_is_rendered_for_new_campaign(self):
        self.create_campaign_through_view()

        response = self.client.get(reverse("home"))

        self.assertContains(response, "The adventure begins")
        self.assertContains(response, "Light rain falls")

    def test_main_quest_finish_completes_and_locks_campaign(self):
        self.create_campaign_through_view()
        campaign = DndSession.objects.get(status=DndSession.Status.ACTIVE)

        completed = finish_quest(campaign.main_quest)

        self.assertTrue(completed)
        campaign.refresh_from_db()
        campaign.main_quest.refresh_from_db()
        self.assertEqual(campaign.status, DndSession.Status.COMPLETED)
        self.assertEqual(campaign.main_quest.status, QuestInstance.Status.FINISHED)
        response = self.client.get(reverse("home"))
        self.assertContains(response, "Adventure complete")
        self.assertContains(response, "completed adventure is read-only")
        self.assertNotContains(response, 'id="open-quit-session"')

    def test_quit_abandons_only_current_users_campaign(self):
        self.create_campaign_through_view()
        campaign = DndSession.objects.get(user__email="player@example.com")
        other_user = AppUser.objects.create(email="other@example.com")
        other_campaign = DndSession.objects.create(
            user=other_user,
            status=DndSession.Status.ACTIVE,
        )

        response = self.client.post(reverse("quit_session"))

        self.assertRedirects(response, reverse("home"))
        campaign.refresh_from_db()
        other_campaign.refresh_from_db()
        self.assertEqual(campaign.status, DndSession.Status.ABANDONED)
        self.assertEqual(other_campaign.status, DndSession.Status.ACTIVE)

    def test_user_cannot_have_two_active_campaigns(self):
        app_user = AppUser.objects.get(email="player@example.com")
        DndSession.objects.create(user=app_user, status=DndSession.Status.ACTIVE)

        with self.assertRaises(IntegrityError), transaction.atomic():
            DndSession.objects.create(
                user=app_user,
                status=DndSession.Status.ACTIVE,
            )


class CharacterTemplateContractTests(SimpleTestCase):
    def test_character_templates_include_shared_knowledge_and_non_combat_options(self):
        templates = load_character_templates()
        item_templates = load_item_templates()

        self.assertEqual(len(templates), 5)
        for template_key, character in templates.items():
            with self.subTest(template=template_key):
                gear_keys = {item["key"] for item in character["gear"]}
                self.assertIn("fairy_of_knowledge", gear_keys)
                self.assertTrue(gear_keys.issubset(item_templates))
                self.assertTrue(
                    any(
                        ability.get("category") == "non_combat"
                        for ability in character["abilities"]
                    )
                )
                self.assertIn("hp", character)
                self.assertIn("ac", character)
                self.assertIn("initiative_modifier", character)
        for item in item_templates.values():
            self.assertTrue(item["public_info"]["summary"])
            self.assertIn("consumable", item["mechanics"])
            self.assertIn("transferable", item["mechanics"])


class DeterministicGameToolTests(TestCase):
    def setUp(self):
        synchronize_fixture()
        auth_user = User.objects.create_user(
            username="tools@example.com",
            email="tools@example.com",
        )
        self.client.force_login(auth_user)
        self.client.post(
            reverse("home"),
            {
                "selected_templates": ["warrior", "druid", "bard"],
                "name_warrior": "Alden",
                "name_druid": "Briar",
                "name_bard": "Calla",
            },
        )
        self.campaign = DndSession.objects.get(status=DndSession.Status.ACTIVE)
        self.turn = CampaignTurn.objects.create(
            dnd_session=self.campaign,
            turn_number=1,
            player_input="Resolve the party command.",
            status=CampaignTurn.Status.RUNNING,
        )
        self.agent_run = AgentRun.objects.create(
            campaign_turn=self.turn,
            model="test-model",
        )

    def call_tool(self, call_id, tool_name, arguments):
        return execute_game_tool(
            agent_run=self.agent_run,
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
        )

    def test_distinct_advantages_stack_and_rolls_are_idempotent(self):
        warrior = self.campaign.characters.get(name="Alden")
        applicability = {
            "roll_types": ["ability_check"],
            "attributes": ["strength"],
            "skills": ["athletics"],
        }
        for number in (1, 2):
            result = self.call_tool(
                f"modifier-{number}",
                "add_roll_modifier",
                {
                    "actor": {"type": "character", "id": warrior.id},
                    "kind": "advantage",
                    "source_key": f"situational.high_ground.{number}",
                    "reason": f"Distinct favorable circumstance {number}",
                    "applies_to": applicability,
                    "duration": "next_applicable_roll",
                },
            )
            self.assertTrue(result.success)
            self.assertTrue(result.data["created"])

        with patch("core.game_tools._roll_faces", return_value=[4, 18, 11]):
            result = self.call_tool(
                "strength-check",
                "roll_check",
                {
                    "actor": {"type": "character", "id": warrior.id},
                    "attribute": "strength",
                    "skill": "athletics",
                    "dc": 15,
                    "reason": "Lift the fallen beam",
                },
            )

        self.assertEqual(result.data["net_advantage"], 2)
        self.assertEqual(result.data["faces"], [4, 18, 11])
        self.assertEqual(result.data["attribute_modifier"], 3)
        self.assertEqual(result.data["training_bonus"], 2)
        self.assertEqual(result.data["total"], 23)
        self.assertTrue(result.data["success"])
        warrior.refresh_from_db()
        self.assertEqual(warrior.modifiers_json, [])

        repeated = self.call_tool(
            "strength-check",
            "roll_check",
            {
                "actor": {"type": "character", "id": warrior.id},
                "attribute": "strength",
                "skill": "athletics",
                "dc": 15,
                "reason": "Lift the fallen beam",
            },
        )
        self.assertEqual(repeated.data, result.data)
        self.assertEqual(self.agent_run.tool_calls.count(), 3)

    def test_dice_saves_and_contests_use_authoritative_mechanics(self):
        warrior = self.campaign.characters.get(name="Alden")
        npc = self.campaign.npcs.get()
        with patch("core.game_tools._roll_faces", return_value=[2, 3]):
            dice = self.call_tool(
                "raw-dice",
                "roll_dice",
                {"dice": "2d6+1", "reason": "A defined random effect"},
            )
        self.assertEqual(dice.data["total"], 6)

        with patch("core.game_tools._roll_faces", return_value=[10]):
            save = self.call_tool(
                "strong-save",
                "roll_save",
                {
                    "actor": {"type": "character", "id": warrior.id},
                    "attribute": "strength",
                    "dc": 12,
                    "reason": "Hold the gate",
                },
            )
        self.assertEqual(save.data["attribute_modifier"], 3)
        self.assertEqual(save.data["strong_save_bonus"], 2)
        self.assertEqual(save.data["total"], 15)

        with patch("core.game_tools._roll_faces", side_effect=[[5], [2]]):
            contest = self.call_tool(
                "contest",
                "roll_contest",
                {
                    "actor": {"type": "character", "id": warrior.id},
                    "actor_attribute": "strength",
                    "actor_skill": "athletics",
                    "opponent": {"type": "npc", "id": npc.id},
                    "opponent_attribute": "strength",
                    "reason": "Force the stuck door from opposite sides",
                },
            )
        self.assertEqual(
            contest.data["winner"],
            {"type": "character", "id": warrior.id},
        )

    def test_movement_ability_rest_and_inventory_mutate_audited_state(self):
        warrior = self.campaign.characters.get(name="Alden")
        npc = self.campaign.npcs.get()
        destination = LocationInstance.objects.create(
            dnd_session=self.campaign,
            name="Old Farmhouse",
            status=LocationInstance.Status.ACTIVE,
        )
        moved_character = self.call_tool(
            "move-character",
            "move_character",
            {
                "character_id": warrior.id,
                "destination_location_id": destination.id,
                "reason": "The route is possible in the established fiction.",
            },
        )
        moved_npc = self.call_tool(
            "move-npc",
            "move_npc",
            {
                "npc_id": npc.id,
                "destination_location_id": destination.id,
                "reason": "The sheriff follows the party.",
            },
        )
        self.assertTrue(moved_character.world_event_id)
        self.assertTrue(moved_npc.world_event_id)
        warrior.refresh_from_db()
        npc.refresh_from_db()
        self.assertEqual(warrior.current_location, destination)
        self.assertEqual(npc.current_location, destination)

        ability = AbilityInstance.objects.get(
            character=warrior,
            template__ability_key="heroic_effort",
        )
        used = self.call_tool(
            "use-ability",
            "use_ability",
            {
                "ability_instance_id": ability.id,
                "reason": "Prepare to lift a heavy beam.",
            },
        )
        self.assertEqual(used.data["remaining_uses"], 0)
        warrior.refresh_from_db()
        self.assertEqual(len(warrior.modifiers_json), 1)
        warrior.mechanics_json["hp"]["current"] = 1
        warrior.save(update_fields=["mechanics_json", "updated_at"])

        rested = self.call_tool(
            "rest",
            "rest_character",
            {"character_id": warrior.id, "reason": "A complete uninterrupted rest."},
        )
        ability.refresh_from_db()
        warrior.refresh_from_db()
        self.assertEqual(
            warrior.mechanics_json["hp"]["current"],
            warrior.mechanics_json["hp"]["maximum"],
        )
        self.assertEqual(ability.remaining_uses, ability.template.max_uses)
        self.assertEqual(warrior.modifiers_json, [])
        self.assertTrue(rested.world_event_id)

        torch = self.campaign.items.get(
            owner_type=ItemInstance.OwnerType.CHARACTER,
            owner_id=warrior.id,
            template__template_key="torch",
        )
        transferred = self.call_tool(
            "transfer-torch",
            "transfer_item",
            {
                "item_instance_id": torch.id,
                "new_owner": {"type": "npc", "id": npc.id},
                "reason": "Alden hands the torch to the sheriff.",
            },
        )
        self.assertTrue(transferred.world_event_id)
        updated = self.call_tool(
            "wet-torch",
            "update_item_state",
            {
                "item_instance_id": torch.id,
                "state_patch": {
                    "public_info": {"wet": True},
                    "dm_only": {"will_light_after_drying": True},
                },
                "reason": "Rain soaked the torch.",
            },
        )
        self.assertTrue(updated.data["state_json"]["public_info"]["wet"])
        consumed = self.call_tool(
            "consume-torch",
            "consume_item",
            {"item_instance_id": torch.id, "reason": "The torch burns away."},
        )
        torch.refresh_from_db()
        self.assertTrue(consumed.world_event_id)
        self.assertEqual(torch.status, ItemInstance.Status.CONSUMED)

    def test_entity_state_and_quest_completion_use_validated_tools(self):
        npc = self.campaign.npcs.get()
        updated = self.call_tool(
            "npc-state",
            "update_entity_state",
            {
                "entity": {"type": "npc", "id": npc.id},
                "state_patch": {
                    "public_info": {"mood": "relieved"},
                    "dm_only": {"remaining_doubt": "Ralavaz's motive"},
                },
                "reason": "The party produced convincing evidence.",
            },
        )
        npc.refresh_from_db()
        self.assertEqual(npc.state_json["public_info"]["mood"], "relieved")
        self.assertTrue(updated.world_event_id)

        finished = self.call_tool(
            "finish-main-quest",
            "finish_quest",
            {
                "quest_id": self.campaign.main_quest_id,
                "reason": "The party discovered the leader and motive.",
            },
        )
        self.campaign.refresh_from_db()
        self.assertTrue(finished.data["campaign_completed"])
        self.assertEqual(self.campaign.status, DndSession.Status.COMPLETED)


class ExistingInterfaceTests(TestCase):
    def test_home_prompts_anonymous_users_to_log_in(self):
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "AI Dungeon Master")
        self.assertContains(response, "Log in with Google")

    def test_create_game_requires_complete_active_scenario(self):
        user = User.objects.create_user(
            username="player@example.com",
            email="player@example.com",
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("home"),
            {
                "selected_templates": ["warrior", "druid", "bard"],
                "name_warrior": "Alden",
                "name_druid": "Briar",
                "name_bard": "Calla",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The adventure has not been prepared yet")
        self.assertFalse(DndSession.objects.exists())

    def test_character_template_sync_still_restores_templates(self):
        CharacterTemplate.objects.filter(template_key="rogue").delete()
        CharacterTemplate.objects.filter(template_key="wizard").update(
            character_template={"class": "Incorrect"}
        )

        sync_character_templates()

        expected_templates = load_character_templates()
        self.assertEqual(
            CharacterTemplate.objects.get(template_key="rogue").character_template,
            expected_templates["rogue"],
        )
        self.assertEqual(
            CharacterTemplate.objects.get(template_key="wizard").character_template,
            expected_templates["wizard"],
        )

    def test_gameplay_template_sync_embeds_abilities_and_items(self):
        with patch(
            "core.scenario_lore.embed_scenario_inputs",
            side_effect=embedded_fixture,
        ):
            ability_count, item_count = sync_gameplay_template_embeddings()

        self.assertEqual(
            AbilityTemplate.objects.filter(active=True, embedding__isnull=False).count(),
            ability_count,
        )
        self.assertEqual(
            ItemTemplate.objects.filter(
                active=True,
                public_embedding__isnull=False,
            ).count(),
            item_count,
        )
