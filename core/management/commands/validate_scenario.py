from django.core.management.base import BaseCommand, CommandError

from core.scenario_lore import ScenarioLoreError, validate_scenario_release


class Command(BaseCommand):
    help = "Parse and validate a scenario release without changing the database."

    def add_arguments(self, parser):
        parser.add_argument("--scenario-dir")
        parser.add_argument("--scenario-key")
        parser.add_argument("--scenario-version", type=int)

    def handle(self, *args, **options):
        try:
            release = validate_scenario_release(
                scenario_dir=options["scenario_dir"],
                scenario_key=options["scenario_key"],
                version=options["scenario_version"],
            )
        except ScenarioLoreError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Validated {len(release.definitions)} definitions for "
                f"{release.scenario_key} v{release.version}."
            )
        )
