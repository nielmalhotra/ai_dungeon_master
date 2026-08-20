from django.core.management.base import BaseCommand

from core.character_templates import sync_gameplay_template_embeddings


class Command(BaseCommand):
    help = "Synchronize and embed character ability and item templates."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=100)

    def handle(self, *args, **options):
        ability_count, item_count = sync_gameplay_template_embeddings(
            batch_size=options["batch_size"]
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Synchronized {ability_count} abilities and {item_count} items."
            )
        )
