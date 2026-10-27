"""python manage.py import_json path/to/file.json [--dry-run] [--wipe]

File format: see core/importer.py.
"""
import json

from django.core.management.base import BaseCommand, CommandError

from core.importer import ImportError_, import_data, wipe_operational_data


class Command(BaseCommand):
    help = "Import projects and loads from a JSON file."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--wipe", action="store_true", help="先删除所有客户 / 项目 / Load（测试用）")

    def handle(self, path, dry_run=False, wipe=False, **opts):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise CommandError(f"无法读取 {path}: {e}")
        try:
            if wipe and not dry_run:
                self.stdout.write(wipe_operational_data())
            for line in import_data(data, dry_run=dry_run):
                self.stdout.write(line)
        except ImportError_ as e:
            raise CommandError(str(e))
