"""Models in a sub-package of an app keep the app's label (#266).

Django labels a model by the app whose package contains the module, not by
the directory the file sits in. label-studio keeps ``S3ImportStorage`` in
``io_storages/s3/models.py`` and re-exports it from ``io_storages/models.py``;
Django calls it ``io_storages.S3ImportStorage``. orm-lens used to call the app
``s3``, so ``describe io_storages.S3ImportStorage`` failed and ``drift``
reported the migrated model as undeclared.
"""

from __future__ import annotations

import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from orm_lens.drift import detect_drift
from orm_lens.parser import scan_workspace

MODEL = """
from django.db import models


class {name}(models.Model):
    bucket = models.TextField(null=True, blank=True)
    prefix = models.TextField(null=True, blank=True)
"""

MIGRATION = """
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="{name}",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("bucket", models.TextField(null=True, blank=True)),
                ("prefix", models.TextField(null=True, blank=True)),
            ],
        ),
    ]
"""


def _write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")


def _labels(root: Path) -> set[str]:
    index = scan_workspace(str(root))
    return {f"{app.name}.{m.name}" for app in index.apps for m in app.models}


class SubPackageAppLabelTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_model_in_a_sub_package_belongs_to_the_enclosing_app(self) -> None:
        app = self.root / "io_storages"
        _write(app / "__init__.py")
        _write(app / "migrations" / "__init__.py")
        _write(app / "s3" / "__init__.py")
        _write(app / "models.py", "from .s3.models import S3ImportStorage  # noqa: F401\n")
        _write(app / "s3" / "models.py", MODEL.format(name="S3ImportStorage"))
        _write(
            app / "migrations" / "0001_initial.py",
            MIGRATION.format(name="S3ImportStorage"),
        )

        self.assertEqual(_labels(self.root), {"io_storages.S3ImportStorage"})
        report = detect_drift(self.root)
        self.assertEqual(report.drifted, [])

    def test_app_with_apps_py_and_no_migrations_also_counts(self) -> None:
        app = self.root / "io_storages"
        _write(app / "__init__.py")
        _write(app / "apps.py")
        _write(app / "gcs" / "__init__.py")
        _write(app / "gcs" / "models.py", MODEL.format(name="GCSImportStorage"))

        self.assertEqual(_labels(self.root), {"io_storages.GCSImportStorage"})

    def test_plain_app_and_models_package_keep_their_labels(self) -> None:
        shop = self.root / "shop"
        _write(shop / "__init__.py")
        _write(shop / "migrations" / "__init__.py")
        _write(shop / "models.py", MODEL.format(name="Product"))
        orders = self.root / "orders"
        _write(orders / "__init__.py")
        _write(orders / "migrations" / "__init__.py")
        _write(orders / "models" / "__init__.py", "from .order import Order  # noqa: F401\n")
        _write(orders / "models" / "order.py", MODEL.format(name="Order"))

        self.assertEqual(_labels(self.root), {"shop.Product", "orders.Order"})

    def test_sub_package_of_an_app_with_its_own_migrations_keeps_its_label(self) -> None:
        outer = self.root / "outer"
        _write(outer / "__init__.py")
        _write(outer / "migrations" / "__init__.py")
        inner = outer / "inner"
        _write(inner / "__init__.py")
        _write(inner / "migrations" / "__init__.py")
        _write(inner / "models.py", MODEL.format(name="Thing"))

        self.assertEqual(_labels(self.root), {"inner.Thing"})

    def test_no_app_marker_above_keeps_the_directory_label(self) -> None:
        pkg = self.root / "lib"
        _write(pkg / "__init__.py")
        _write(pkg / "storage" / "__init__.py")
        _write(pkg / "storage" / "models.py", MODEL.format(name="Blob"))

        self.assertEqual(_labels(self.root), {"storage.Blob"})

    def test_walk_stops_at_a_directory_that_is_not_a_package(self) -> None:
        # `site/` has migrations/ but is not a package, so `blog/` (a package
        # with no markers of its own) must not be pulled into it.
        site = self.root / "site"
        _write(site / "migrations" / "__init__.py")
        _write(site / "blog" / "__init__.py")
        _write(site / "blog" / "models.py", MODEL.format(name="Post"))

        self.assertEqual(_labels(self.root), {"blog.Post"})


if __name__ == "__main__":
    unittest.main()
