from django.core.management.base import BaseCommand

from core.digest import send_all


class Command(BaseCommand):
    help = "Send today's to-do e-mail to everyone who has it switched on."

    def handle(self, *args, **options):
        self.stdout.write(str(send_all()))
