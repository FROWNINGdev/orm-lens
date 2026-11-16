"""Tests for resolving app labels for models in sub-packages (issue #266).

When models are split into sub-packages within an app
(e.g. ``io_storages/s3/models.py`` with ``io_storages/apps.py``), the Django app
label is ``io_storages``, not ``s3``.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orm_lens._paths import _app_dir_for
from orm_lens.drift import detect_drift
from orm_lens.parser import parse_models_file, scan_workspace


class SubpackageAppLabelTests(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)

    def test_app_dir_for_subpackage_resolves_to_app_root(self) -> None:
        app_dir = self.root / "io_storages"
        sub_dir = app_dir / "s3"
        sub_dir.mkdir(parents=True)
        (app_dir / "__init__.py").write_text("", encoding="utf-8")
        (app_dir / "apps.py").write_text("", encoding="utf-8")
        (sub_dir / "__init__.py").write_text("", encoding="utf-8")

        models_file = sub_dir / "models.py"
        resolved_dir, app_name = _app_dir_for(str(models_file))
        self.assertEqual(resolved_dir, str(app_dir))
        self.assertEqual(app_name, "io_storages")

    def test_app_dir_for_nested_subpackage_resolves_to_app_root(self) -> None:
        app_dir = self.root / "io_storages"
        deep_dir = app_dir / "providers" / "s3"
        deep_dir.mkdir(parents=True)
        (app_dir / "__init__.py").write_text("", encoding="utf-8")
        (app_dir / "migrations").mkdir()
        (app_dir / "migrations" / "__init__.py").write_text("", encoding="utf-8")
        (app_dir / "providers" / "__init__.py").write_text("", encoding="utf-8")
        (deep_dir / "__init__.py").write_text("", encoding="utf-8")

        models_file = deep_dir / "models.py"
        resolved_dir, app_name = _app_dir_for(str(models_file))
        self.assertEqual(resolved_dir, str(app_dir))
        self.assertEqual(app_name, "io_storages")

    def test_app_dir_for_subpackage_with_models_folder(self) -> None:
        app_dir = self.root / "io_storages"
        models_pkg = app_dir / "s3" / "models"
        models_pkg.mkdir(parents=True)
        (app_dir / "__init__.py").write_text("", encoding="utf-8")
        (app_dir / "apps.py").write_text("", encoding="utf-8")
        (app_dir / "s3" / "__init__.py").write_text("", encoding="utf-8")
        (models_pkg / "__init__.py").write_text("", encoding="utf-8")

        storage_file = models_pkg / "storage.py"
        resolved_dir, app_name = _app_dir_for(str(storage_file))
        self.assertEqual(resolved_dir, str(app_dir))
        self.assertEqual(app_name, "io_storages")

    def test_app_dir_for_plain_app_and_models_package(self) -> None:
        shop_dir = self.root / "shop"
        (shop_dir / "models").mkdir(parents=True)
        (shop_dir / "__init__.py").write_text("", encoding="utf-8")
        (shop_dir / "apps.py").write_text("", encoding="utf-8")

        plain_file = shop_dir / "models.py"
        resolved_dir, app_name = _app_dir_for(str(plain_file))
        self.assertEqual(resolved_dir, str(shop_dir))
        self.assertEqual(app_name, "shop")

        split_file = shop_dir / "models" / "order.py"
        resolved_dir, app_name = _app_dir_for(str(split_file))
        self.assertEqual(resolved_dir, str(shop_dir))
        self.assertEqual(app_name, "shop")

    def test_app_dir_for_unmarked_directory_keeps_basename(self) -> None:
        unmarked = self.root / "standalone" / "pkg"
        unmarked.mkdir(parents=True)
        # No apps.py or migrations/ in unmarked or its parents
        models_file = unmarked / "models.py"
        resolved_dir, app_name = _app_dir_for(str(models_file))
        self.assertEqual(resolved_dir, str(unmarked))
        self.assertEqual(app_name, "pkg")

    def test_parse_models_file_subpackage_gets_parent_app_label(self) -> None:
        app_dir = self.root / "io_storages"
        sub_dir = app_dir / "s3"
        sub_dir.mkdir(parents=True)
        (app_dir / "__init__.py").write_text("", encoding="utf-8")
        (app_dir / "apps.py").write_text("", encoding="utf-8")
        (sub_dir / "__init__.py").write_text("", encoding="utf-8")

        s3_file = sub_dir / "models.py"
        content = (
            "from django.db import models\n\n\n"
            "class S3ImportStorage(models.Model):\n"
            "    bucket = models.TextField(null=True, blank=True)\n"
            "    prefix = models.TextField(null=True, blank=True)\n"
        )
        s3_file.write_text(content, encoding="utf-8")

        models = parse_models_file(str(s3_file), content)
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].name, "S3ImportStorage")
        self.assertEqual(models[0].app_name, "io_storages")

    def test_repro_scan_workspace_and_drift_no_false_positive(self) -> None:
        # Repro structure from issue #266:
        # io_storages/
        #     __init__.py
        #     apps.py
        #     migrations/
        #         __init__.py
        #         0001_initial.py
        #     models.py
        #     s3/
        #         __init__.py
        #         models.py
        app_dir = self.root / "io_storages"
        s3_dir = app_dir / "s3"
        mig_dir = app_dir / "migrations"
        s3_dir.mkdir(parents=True)
        mig_dir.mkdir(parents=True)

        (app_dir / "__init__.py").write_text("", encoding="utf-8")
        (app_dir / "apps.py").write_text("", encoding="utf-8")
        (s3_dir / "__init__.py").write_text("", encoding="utf-8")
        (mig_dir / "__init__.py").write_text("", encoding="utf-8")

        (app_dir / "models.py").write_text(
            "from .s3.models import S3ImportStorage  # noqa: F401\n",
            encoding="utf-8",
        )

        s3_content = (
            "from django.db import models\n\n\n"
            "class S3ImportStorage(models.Model):\n"
            "    bucket = models.TextField(null=True, blank=True)\n"
            "    prefix = models.TextField(null=True, blank=True)\n"
        )
        (s3_dir / "models.py").write_text(s3_content, encoding="utf-8")

        migration_content = (
            "from django.db import migrations, models\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    initial = True\n"
            "    dependencies = []\n"
            "    operations = [\n"
            "        migrations.CreateModel(\n"
            '            name="S3ImportStorage",\n'
            "            fields=[\n"
            '                ("id", models.AutoField(primary_key=True, serialize=False)),\n'
            '                ("bucket", models.TextField(null=True, blank=True)),\n'
            '                ("prefix", models.TextField(null=True, blank=True)),\n'
            "            ],\n"
            "        ),\n"
            "    ]\n"
        )
        (mig_dir / "0001_initial.py").write_text(migration_content, encoding="utf-8")

        # 1. Scan workspace
        index = scan_workspace(str(self.root))
        self.assertEqual([a.name for a in index.apps], ["io_storages"])
        app = index.apps[0]
        self.assertEqual(len(app.models), 1)
        self.assertEqual(app.models[0].name, "S3ImportStorage")
        self.assertEqual(app.models[0].app_name, "io_storages")

        # 2. Check drift: should find 0 drifted models
        drift = detect_drift(str(self.root))
        self.assertEqual(len(drift.drifted), 0)
        self.assertEqual(drift.migrations_replayed, 1)
