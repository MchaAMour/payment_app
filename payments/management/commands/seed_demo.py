import os
import secrets

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ("Create demo accounts: a regular buyer and a staff user. Idempotent: existing users are "
            "left alone unless --reset is given. A password is generated (and printed once) unless "
            "--password or $DEMO_PASSWORD is set.")

    def add_arguments(self, parser):
        parser.add_argument("--buyer", default="demo")
        parser.add_argument("--staff", default="staff")
        parser.add_argument("--password", default=os.environ.get("DEMO_PASSWORD", ""))
        parser.add_argument("--reset", action="store_true", help="reset passwords of existing users")

    def handle(self, *args, **opts):
        User = get_user_model()
        password = opts["password"] or secrets.token_urlsafe(12)
        generated = not opts["password"]

        for username, is_staff in ((opts["buyer"], False), (opts["staff"], True)):
            user, created = User.objects.get_or_create(username=username)
            if created or opts["reset"]:
                user.set_password(password)
                user.is_staff = is_staff
                user.is_superuser = is_staff  # staff can use /admin and see ops panels
                user.save()
                shown = password if generated else "(as given)"
                self.stdout.write(f"{'created' if created else 'reset'} {username}"
                                  f"{' [staff]' if is_staff else ''}  password: {shown}")
            else:
                self.stdout.write(f"{username} already exists (password unchanged)")
