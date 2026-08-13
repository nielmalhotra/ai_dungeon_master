from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

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
    WorldLore,
    WorldLoreChunkTemplate,
)
from .scenario_lore import (
    EmbeddedScenarioChunk,
    ScenarioChunk,
    ScenarioLoreError,
    embed_scenario_chunks,
    load_scenario_chunks,
    sync_world_lore_chunk_templates,
)


def create_lore_template(**overrides):
    values = {
        "scenario_key": "whitesparrow",
        "version": "1.0",
        "active": True,
        "source_file": "main.txt",
        "section": "The Night Blades of Whitesparrow",
        "chunk_number": 1,
        "content": "A rainy road leads to Whitesparrow.",
        "metadata": {"visibility": "player"},
        "embedding": [0.0] * 3072,
    }
    values.update(overrides)
    return WorldLoreChunkTemplate.objects.create(**values)


def embedded_lore_fixture(chunks):
    return [
        EmbeddedScenarioChunk(chunk=chunk, embedding=[0.0] * 3072)
        for chunk in chunks
    ]


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

    def test_create_game_saves_character_and_lore_snapshots(self):
        self.login()
        lore_template = create_lore_template()
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
        dnd_session = DndSession.objects.get(active=True)
        self.assertEqual(dnd_session.user.email, "player@example.com")
        self.assertEqual(dnd_session.characters.count(), 3)
        self.assertEqual(dnd_session.world_lore.count(), 1)
        lore = dnd_session.world_lore.get()
        self.assertEqual(lore.version, "1.0")
        self.assertEqual(lore.source_file, "main.txt")
        self.assertEqual(lore.metadata["visibility"], "player")
        lore_template.content = "Changed after session creation."
        lore_template.save(update_fields=["content"])
        lore.refresh_from_db()
        self.assertEqual(lore.content, "A rainy road leads to Whitesparrow.")
        warrior = dnd_session.characters.get(name="Alden")
        self.assertEqual(warrior.template_json, original_warrior)
        self.assertNotIn("name", warrior.template_json)

        CharacterTemplate.objects.filter(template_key="warrior").update(
            character_template={"class": "Changed"}
        )
        warrior.refresh_from_db()
        self.assertEqual(warrior.template_json, original_warrior)

    def test_create_game_requires_prebuilt_lore_templates(self):
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
    def test_scenario_files_share_version_and_create_semantic_chunks(self):
        chunks = load_scenario_chunks()

        self.assertGreater(len(chunks), 20)
        self.assertEqual({chunk.version for chunk in chunks}, {"1.0"})
        self.assertEqual(chunks[0].source_file, "main.txt")
        self.assertEqual(chunks[0].metadata["visibility"], "player")
        self.assertTrue(
            all(
                chunk.metadata["visibility"] == "game_master"
                for chunk in chunks
                if chunk.source_file != "main.txt"
            )
        )
        self.assertIn("Mud Pit", {chunk.section for chunk in chunks})
        self.assertNotIn("ATTRIBUTION.txt", {chunk.source_file for chunk in chunks})

    def test_mismatched_file_version_is_rejected(self):
        with TemporaryDirectory() as directory:
            scenario_dir = Path(directory)
            (scenario_dir / "main.txt").write_text(
                "Version 1.0\n# Main\n\nThe public premise.",
                encoding="utf-8",
            )
            (scenario_dir / "private.txt").write_text(
                "Version 1.1\n# Secret\n\nThe private truth.",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ScenarioLoreError, "expected Version 1.0"):
                load_scenario_chunks(scenario_dir)

    def test_embedding_requests_are_batched_and_validated(self):
        chunks = load_scenario_chunks()

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

        embedded = embed_scenario_chunks(chunks, client=client, batch_size=10)

        self.assertEqual(create_embedding.call_count, 4)
        request = create_embedding.call_args_list[0].kwargs
        self.assertEqual(request["model"], "text-embedding-3-large")
        self.assertEqual(request["dimensions"], 3072)
        self.assertEqual(len(request["input"]), 10)
        self.assertEqual(len(embedded), len(chunks))
        self.assertEqual(len(embedded[0].embedding), 3072)

    @patch("core.scenario_lore.embed_scenario_chunks")
    def test_template_sync_activates_new_version_and_keeps_old_version(
        self,
        embed_chunks,
    ):
        old_template = create_lore_template(version="0.9")
        chunks = load_scenario_chunks()
        embed_chunks.return_value = embedded_lore_fixture(chunks)

        version, count = sync_world_lore_chunk_templates()

        self.assertEqual(version, "1.0")
        self.assertEqual(count, len(chunks))
        old_template.refresh_from_db()
        self.assertFalse(old_template.active)
        self.assertEqual(
            WorldLoreChunkTemplate.objects.filter(
                scenario_key="whitesparrow",
                version="1.0",
                active=True,
            ).count(),
            len(chunks),
        )
