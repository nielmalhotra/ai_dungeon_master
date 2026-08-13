from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from accounts.models import User as AppUser

from .character_templates import load_character_templates, sync_character_templates
from .models import (
    CharacterInstance,
    CharacterTemplate,
    DndSession,
    LocationTemplate,
    NPCTemplate,
    QuestTemplate,
    Visibility,
    WorldLore,
    WorldLoreChunkTemplate,
)
from .scenario_lore import (
    ScenarioLoreError,
    build_scenario_embedding_inputs,
    embed_scenario_inputs,
    load_scenario_release,
    sync_scenario_templates,
)


ZERO_EMBEDDING = [0.0] * 3072
LOCATION_UUID = UUID("94546ab4-3d58-40e2-af49-70753e784f25")
NPC_UUID = UUID("db1041ba-2f74-44db-ba91-ef7408afff1a")
QUEST_UUID = UUID("4b802d31-63b9-4467-9710-b7b5b2c0b793")
LORE_UUID = UUID("d5218545-8822-4b1e-8b76-d5196cc584dc")


def visibility_state(public_summary, dm_summary):
    return {
        "public_info": {"summary": public_summary},
        "dm_only": {"summary": dm_summary},
    }


def create_scenario_templates(version=1, active=True):
    location = LocationTemplate.objects.create(
        definition_uuid=LOCATION_UUID,
        scenario_key="whitesparrow",
        version=version,
        active=active,
        source_file="locations/whitesparrow_village.txt",
        name="Whitesparrow Village",
        is_starting_location=True,
        initially_known=True,
        definition_json=visibility_state(
            "Whitesparrow is a mountain village.",
            "The villagers are suspicious of outsiders.",
        ),
        public_embedding=ZERO_EMBEDDING,
        dm_embedding=ZERO_EMBEDDING,
    )
    npc = NPCTemplate.objects.create(
        definition_uuid=NPC_UUID,
        scenario_key="whitesparrow",
        version=version,
        active=active,
        source_file="npcs/sheriff_ruth_willowmane.txt",
        name="Sheriff Ruth Willowmane",
        initial_location_template=location,
        initially_known=False,
        definition_json=visibility_state(
            "Ruth is Whitesparrow's sheriff.",
            "Ruth's anger toward Ralavaz is personal.",
        ),
        public_embedding=ZERO_EMBEDDING,
        dm_embedding=ZERO_EMBEDDING,
    )
    quest = QuestTemplate.objects.create(
        definition_uuid=QUEST_UUID,
        scenario_key="whitesparrow",
        version=version,
        active=active,
        source_file="quests/investigate_the_night_blades.txt",
        title="Investigate the Night Blades",
        initial_status=QuestTemplate.InitialStatus.AVAILABLE,
        initially_known=True,
        definition_json=visibility_state(
            "Discover who leads the Night Blades.",
            "The masked Night Lord has a hidden motive.",
        ),
        related_templates_json=[
            {"type": "npc", "id": npc.id},
            {"type": "location", "id": location.id},
        ],
        public_embedding=ZERO_EMBEDDING,
        dm_embedding=ZERO_EMBEDDING,
    )
    lore = WorldLoreChunkTemplate.objects.create(
        definition_uuid=LORE_UUID,
        scenario_key="whitesparrow",
        version=version,
        active=active,
        source_file="worldlore/the_night_blades.txt",
        title="The Night Blades",
        section="Public Info",
        chunk_number=1,
        visibility=Visibility.PUBLIC_INFO,
        initially_known=True,
        content="The Night Blades once terrorized the valley.",
        metadata_json={
            "related_templates": [
                {"type": "npc", "id": npc.id},
                {"type": "location", "id": location.id},
            ]
        },
        embedding=ZERO_EMBEDDING,
    )
    return {
        "location": location,
        "npc": npc,
        "quest": quest,
        "lore": lore,
    }


class HomeViewTests(TestCase):
    def login(self):
        user = User.objects.create_user(
            username="player@example.com",
            email="player@example.com",
        )
        self.client.force_login(user)
        return user

    def create_active_session(self):
        app_user = AppUser.objects.get(email="player@example.com")
        dnd_session = DndSession.objects.create(user=app_user, active=True)
        for template_key, name in (
            ("warrior", "Alden"),
            ("rogue", "Brynn"),
            ("wizard", "Cora"),
        ):
            template = CharacterTemplate.objects.get(template_key=template_key)
            CharacterInstance.objects.create(
                dnd_session=dnd_session,
                name=name,
                template_json=deepcopy(template.character_template),
            )
        return dnd_session

    def test_home_prompts_anonymous_users_to_log_in(self):
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "AI Dungeon Master")
        self.assertContains(response, "Log in with Google")
        self.assertContains(response, "/accounts/google/login/")

    def test_home_prompts_authenticated_user_without_game_to_create_party(self):
        self.login()

        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create your party")
        self.assertContains(response, "simplified version of D&amp;D fifth edition")
        self.assertContains(response, 'type="checkbox"', count=5)
        self.assertContains(response, "Choose and name exactly three characters")
        self.assertNotContains(response, 'id="open-characters"')

    def test_home_shows_chat_and_characters_for_active_session(self):
        self.login()
        self.create_active_session()

        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Adventure")
        self.assertContains(response, "textarea disabled")
        self.assertContains(response, 'id="open-characters"')
        self.assertContains(response, 'id="characters-dialog"')
        self.assertContains(response, 'id="open-quit-session"')
        self.assertContains(response, 'id="quit-session-dialog"')
        self.assertContains(response, "Alden")
        self.assertContains(response, "24/24")
        self.assertContains(response, "Catch your breath and restore 1d8+2 HP")

    def test_create_game_requires_exactly_three_named_characters(self):
        self.login()

        response = self.client.post(
            reverse("home"),
            {
                "selected_templates": ["warrior", "rogue"],
                "name_warrior": "Alden",
                "name_rogue": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose exactly three characters.")
        self.assertContains(response, "Enter a name for this character.")
        self.assertFalse(DndSession.objects.exists())

    def test_create_game_instantiates_complete_scenario(self):
        self.login()
        templates = create_scenario_templates()
        original_warrior = deepcopy(
            CharacterTemplate.objects.get(template_key="warrior").character_template
        )

        response = self.client.post(
            reverse("home"),
            {
                "selected_templates": ["warrior", "druid", "bard"],
                "name_warrior": "Alden",
                "name_druid": "Briar",
                "name_bard": "Calla",
            },
        )

        self.assertRedirects(response, reverse("home"))
        campaign = DndSession.objects.get(active=True)
        self.assertEqual(campaign.user.email, "player@example.com")
        self.assertEqual(campaign.scenario_key, "whitesparrow")
        self.assertEqual(campaign.scenario_version, 1)
        self.assertEqual(campaign.locations.count(), 1)
        self.assertEqual(campaign.npcs.count(), 1)
        self.assertEqual(campaign.quests.count(), 1)
        self.assertEqual(campaign.world_lore.count(), 1)
        self.assertEqual(campaign.characters.count(), 3)

        location = campaign.locations.get()
        npc = campaign.npcs.get()
        quest = campaign.quests.get()
        self.assertEqual(campaign.current_location, location)
        self.assertEqual(location.template, templates["location"])
        self.assertEqual(npc.current_location, location)
        self.assertEqual(npc.state_json["public_info"], {})
        self.assertEqual(quest.status, QuestTemplate.InitialStatus.AVAILABLE)
        self.assertEqual(
            quest.related_entities_json,
            [
                {"type": "npc", "id": npc.id},
                {"type": "location", "id": location.id},
            ],
        )
        self.assertEqual(
            campaign.world_lore.get().template,
            templates["lore"],
        )
        self.assertTrue(
            all(
                character.current_location_id == location.id
                for character in campaign.characters.all()
            )
        )
        warrior = campaign.characters.get(name="Alden")
        self.assertEqual(warrior.template_json, original_warrior)
        self.assertEqual(warrior.mechanics_json, original_warrior)

    def test_create_game_requires_complete_active_scenario(self):
        self.login()

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
        self.assertFalse(WorldLore.objects.exists())

    def test_home_includes_current_rules_overlay_for_authenticated_users(self):
        self.login()

        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="open-documentation"')
        self.assertContains(response, 'id="documentation-dialog"')
        self.assertContains(response, "AI DUNGEON MASTER RULESET")
        self.assertContains(response, "Phoenix of Invincibility")


class CharacterTemplateTests(TestCase):
    def test_character_templates_are_loaded_without_names(self):
        self.client.get(reverse("home"))

        self.assertEqual(CharacterTemplate.objects.count(), 5)
        self.assertEqual(
            set(CharacterTemplate.objects.values_list("template_key", flat=True)),
            {"warrior", "rogue", "wizard", "druid", "bard"},
        )
        for template in CharacterTemplate.objects.all():
            self.assertNotIn("name", template.character_template)
            for ability in template.character_template["abilities"]:
                self.assertTrue(ability["explanation"].strip())

    def test_sync_restores_missing_and_changed_character_templates(self):
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


class DndSessionTests(TestCase):
    def test_user_cannot_have_two_active_sessions(self):
        app_user = AppUser.objects.create(email="player@example.com")
        DndSession.objects.create(user=app_user, active=True)

        with self.assertRaises(IntegrityError), transaction.atomic():
            DndSession.objects.create(user=app_user, active=True)

    def test_quit_session_inactivates_only_logged_in_users_session(self):
        auth_user = User.objects.create_user(
            username="player@example.com",
            email="player@example.com",
        )
        self.client.force_login(auth_user)
        app_user = AppUser.objects.get(email="player@example.com")
        current_session = DndSession.objects.create(user=app_user, active=True)

        other_user = AppUser.objects.create(email="other@example.com")
        other_session = DndSession.objects.create(user=other_user, active=True)

        response = self.client.post(reverse("quit_session"))

        self.assertRedirects(response, reverse("home"))
        current_session.refresh_from_db()
        other_session.refresh_from_db()
        self.assertFalse(current_session.active)
        self.assertTrue(other_session.active)


class ScenarioLoreTests(TestCase):
    def test_release_loads_all_definition_types_and_resolves_source_contract(self):
        release = load_scenario_release()

        self.assertEqual(release.scenario_key, "whitesparrow")
        self.assertEqual(release.version, 1)
        self.assertEqual(len(release.definitions), 4)
        self.assertEqual(
            {definition.definition_type for definition in release.definitions},
            {"location", "npc", "quest", "world_lore"},
        )
        self.assertEqual(len(release.lore_chunks), 2)
        self.assertEqual(
            {chunk.visibility for chunk in release.lore_chunks},
            {Visibility.PUBLIC_INFO, Visibility.DM_ONLY},
        )
        self.assertTrue(release.lore_chunks[0].initially_known)
        self.assertFalse(release.lore_chunks[1].initially_known)

        quest = release.definitions_of_type("quest")[0]
        self.assertEqual(
            quest.related_references,
            (("npc", NPC_UUID), ("location", LOCATION_UUID)),
        )

    def test_empty_required_definition_folder_is_rejected(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            release_dir = Path(directory) / "whitesparrow" / "v1"
            for folder in ("locations", "npcs", "quests"):
                (release_dir / folder).mkdir(parents=True, exist_ok=True)

            with self.assertRaisesRegex(ScenarioLoreError, "locations/"):
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
        self.assertEqual(len(next(iter(embedded.values()))), 3072)

    @patch("core.scenario_lore.embed_scenario_inputs")
    def test_template_sync_builds_and_links_complete_active_release(
        self,
        embed_inputs,
    ):
        old_template = WorldLoreChunkTemplate.objects.create(
            definition_uuid=uuid4(),
            scenario_key="whitesparrow",
            version=2,
            active=True,
            source_file="worldlore/old.txt",
            title="Old Lore",
            section="Public Info",
            chunk_number=1,
            visibility=Visibility.PUBLIC_INFO,
            initially_known=True,
            content="Old lore.",
            embedding=ZERO_EMBEDDING,
        )

        def embedded_fixture(inputs, **kwargs):
            return {item.key: ZERO_EMBEDDING for item in inputs}

        embed_inputs.side_effect = embedded_fixture

        version, count = sync_scenario_templates()

        self.assertEqual(version, 1)
        self.assertEqual(count, 5)
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
            quest.related_templates_json,
            [
                {"type": "npc", "id": npc.id},
                {"type": "location", "id": location.id},
            ],
        )

        second_version, second_count = sync_scenario_templates()
        self.assertEqual((second_version, second_count), (1, 5))
        self.assertEqual(LocationTemplate.objects.count(), 1)
        self.assertEqual(NPCTemplate.objects.count(), 1)
        self.assertEqual(QuestTemplate.objects.count(), 1)
        self.assertEqual(WorldLoreChunkTemplate.objects.count(), 3)
