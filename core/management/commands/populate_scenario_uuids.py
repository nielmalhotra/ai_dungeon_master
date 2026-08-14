from django.core.management.base import BaseCommand, CommandError

from core.scenario_lore import ScenarioLoreError, populate_scenario_uuids


class Command(BaseCommand):
    help = "Populate UUID placeholders using exact prior-version matches."

    def add_arguments(self, parser):
        parser.add_argument("--scenario-dir")
        parser.add_argument("--scenario-key")
        parser.add_argument("--scenario-version", type=int)

    def handle(self, *args, **options):
        try:
            populated = populate_scenario_uuids(
                scenario_dir=options["scenario_dir"],
                scenario_key=options["scenario_key"],
                version=options["scenario_version"],
            )
        except ScenarioLoreError as exc:
            raise CommandError(str(exc)) from exc
        for source_file, definition_uuid in populated.items():
            self.stdout.write(f"{source_file}: {definition_uuid}")
        self.stdout.write(
            self.style.SUCCESS(f"Populated {len(populated)} definition UUIDs.")
        )
