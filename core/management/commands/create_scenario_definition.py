from django.core.management.base import BaseCommand, CommandError

from core.scenario_lore import ScenarioLoreError, create_definition_file


class Command(BaseCommand):
    help = "Create a scenario entity text file with a UUID placeholder."

    def add_arguments(self, parser):
        parser.add_argument("release_dir")
        parser.add_argument(
            "entity_type",
            choices=("location", "npc", "quest", "world_lore"),
        )
        parser.add_argument("filename")
        parser.add_argument("name")
        parser.add_argument("--initial-status")

    def handle(self, *args, **options):
        try:
            target = create_definition_file(
                release_dir=options["release_dir"],
                definition_type=options["entity_type"],
                filename=options["filename"],
                name=options["name"],
                initial_status=options["initial_status"],
            )
        except ScenarioLoreError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Created {target}."))
