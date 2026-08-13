from django.core.management.base import BaseCommand, CommandError

from core.scenario_lore import ScenarioLoreError, sync_world_lore_chunk_templates


class Command(BaseCommand):
    help = "Chunk and embed the scenario files into world lore templates."

    def add_arguments(self, parser):
        parser.add_argument("--scenario-dir")
        parser.add_argument("--scenario-key")
        parser.add_argument("--batch-size", type=int, default=100)

    def handle(self, *args, **options):
        try:
            version, count = sync_world_lore_chunk_templates(
                scenario_dir=options["scenario_dir"],
                scenario_key=options["scenario_key"],
                batch_size=options["batch_size"],
            )
        except ScenarioLoreError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"Synced {count} world lore templates for Version {version}."
            )
        )
