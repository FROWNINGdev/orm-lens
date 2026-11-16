"""Regression tests for issue #254 — project-defined Django field subclasses.

Fields declared with custom subclasses of Django model fields (defined in the
same ``models.py`` or imported from project modules such as ``core/fields.py``)
used to be skipped because ``FIELD_RE`` required ``models.X(`` and
``BARE_FIELD_RE`` matched only built-in Django field names.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from orm_lens.cli import main as cli_main
from orm_lens.drift import detect_drift
from orm_lens.parser import parse_models_file, scan_workspace


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class Issue254ReproTest(unittest.TestCase):
    """Exact two-module reproduction from issue #254."""

    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)

        _write(self.root / "core" / "__init__.py", "")
        _write(
            self.root / "core" / "fields.py",
            "from django.db import models\n"
            "from django.db.models import DecimalField\n\n\n"
            "class NormalizedDecimalField(DecimalField):\n"
            "    pass\n\n\n"
            "class RegexField(models.TextField):\n"
            "    pass\n\n\n"
            "class GroupManyToManyField(models.ManyToManyField):\n"
            "    pass\n",
        )
        _write(self.root / "shop" / "__init__.py", "")
        _write(
            self.root / "shop" / "models.py",
            "from django.db import models\n"
            "from core.fields import (\n"
            "    GroupManyToManyField,\n"
            "    NormalizedDecimalField,\n"
            "    RegexField,\n"
            ")\n\n\n"
            "class Group(models.Model):\n"
            "    name = models.CharField(max_length=50)\n\n\n"
            "class Product(models.Model):\n"
            "    name = models.CharField(max_length=100)\n"
            "    tax_rate = NormalizedDecimalField(max_digits=5, decimal_places=2)\n"
            "    key_filter = RegexField(blank=True)\n"
            '    groups = GroupManyToManyField("Group", blank=True)\n',
        )

    def test_scan_workspace_resolves_underlying_field_types_and_relations(self) -> None:
        index = scan_workspace(str(self.root))
        models = {m.name: m for app in index.apps for m in app.models}
        product = models["Product"]

        self.assertEqual(
            [(f.name, f.type) for f in product.fields],
            [
                ("name", "CharField"),
                ("tax_rate", "DecimalField"),
                ("key_filter", "TextField"),
                ("groups", "ManyToManyField"),
            ],
        )
        groups = next(f for f in product.fields if f.name == "groups")
        self.assertTrue(groups.is_relation)
        self.assertEqual(groups.relation_kind, "ManyToManyField")
        self.assertEqual(groups.related_model, "Group")

    def test_cli_describe_lists_custom_fields(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli_main(["describe", "shop.Product", "--path", str(self.root)])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("tax_rate", out)
        self.assertIn("DecimalField", out)
        self.assertIn("key_filter", out)
        self.assertIn("TextField", out)
        self.assertIn("groups", out)
        self.assertIn("ManyToManyField", out)

    def test_drift_matches_migrations_and_flags_unmigrated_custom_field(self) -> None:
        _write(
            self.root / "shop" / "migrations" / "0001_initial.py",
            "from django.db import migrations, models\n"
            "import core.fields\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = []\n"
            "    operations = [\n"
            "        migrations.CreateModel(\n"
            '            name="Group",\n'
            '            fields=[("name", models.CharField(max_length=50))],\n'
            "        ),\n"
            "        migrations.CreateModel(\n"
            '            name="Product",\n'
            "            fields=[\n"
            '                ("name", models.CharField(max_length=100)),\n'
            '                ("tax_rate", core.fields.NormalizedDecimalField(max_digits=5, decimal_places=2)),\n'
            '                ("key_filter", core.fields.RegexField(blank=True)),\n'
            '                ("groups", core.fields.GroupManyToManyField("shop.Group", blank=True)),\n'
            "            ],\n"
            "        ),\n"
            "    ]\n",
        )
        clean = detect_drift(self.root)
        self.assertEqual(clean.drifted, [])
        self.assertEqual(clean.blocking_count, 0)

        # Drop ``tax_rate`` from the migration: drift must block on it.
        _write(
            self.root / "shop" / "migrations" / "0001_initial.py",
            "from django.db import migrations, models\n"
            "import core.fields\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = []\n"
            "    operations = [\n"
            "        migrations.CreateModel(\n"
            '            name="Group",\n'
            '            fields=[("name", models.CharField(max_length=50))],\n'
            "        ),\n"
            "        migrations.CreateModel(\n"
            '            name="Product",\n'
            "            fields=[\n"
            '                ("name", models.CharField(max_length=100)),\n'
            '                ("key_filter", core.fields.RegexField(blank=True)),\n'
            '                ("groups", core.fields.GroupManyToManyField("shop.Group", blank=True)),\n'
            "            ],\n"
            "        ),\n"
            "    ]\n",
        )
        drifted = detect_drift(self.root)
        self.assertEqual(drifted.blocking_count, 1)
        self.assertEqual(drifted.drifted[0].missing_in_migrations, ["tax_rate"])


class SameFileCustomFieldTest(unittest.TestCase):
    """Custom field defined in the same ``models.py`` (e.g. weblate/auth/models.py)."""

    SAMPLE = (
        "from django.db import models\n\n\n"
        "class GroupManyToManyField(models.ManyToManyField):\n"
        "    pass\n\n\n"
        "class SlugCharField(models.CharField[str, str]):\n"
        "    pass\n\n\n"
        "class User(models.Model):\n"
        "    username: str = SlugCharField(max_length=150)\n"
        '    groups = GroupManyToManyField("Group", blank=True)\n'
    )

    def test_parse_models_file_resolves_same_file_subclasses(self) -> None:
        parsed = parse_models_file("accounts/models.py", self.SAMPLE)
        self.assertEqual([m.name for m in parsed], ["User"])
        user = parsed[0]
        self.assertEqual(
            [(f.name, f.type) for f in user.fields],
            [("username", "CharField"), ("groups", "ManyToManyField")],
        )
        self.assertEqual(user.fields[1].related_model, "Group")

    def test_scan_workspace_resolves_same_file_subclasses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root / "accounts" / "models.py", self.SAMPLE)
            index = scan_workspace(str(root))
            models = {m.name: m for app in index.apps for m in app.models}
            self.assertEqual(list(models), ["User"])
            self.assertEqual(
                [(f.name, f.type) for f in models["User"].fields],
                [("username", "CharField"), ("groups", "ManyToManyField")],
            )


class CrossModuleAndInheritanceChainTest(unittest.TestCase):
    """Multi-hop field inheritance, re-exports, ``src/`` layout, FK/O2O, and bases."""

    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)

        # ``src/`` layout like pretix: ``src/acme/...`` scanned from repo root.
        pkg = self.root / "src" / "acme"
        _write(pkg / "__init__.py", "")
        _write(
            pkg / "base" / "fields.py",
            "from django.db import models\n\n\n"
            "class MultiStringField(models.Field):\n"
            "    pass\n\n\n"
            "class I18nCharField(MultiStringField):\n"
            "    pass\n\n\n"
            "class ScopedForeignKey(models.ForeignKey):\n"
            "    pass\n\n\n"
            "class PrimaryOneToOneField(models.OneToOneField):\n"
            "    pass\n",
        )
        _write(
            pkg / "helpers" / "money.py",
            "from django.db.models import DecimalField\n\n\n"
            "class MoneyDecimalField(DecimalField):\n"
            "    pass\n",
        )
        # Package re-export (two hops from importer).
        _write(
            pkg / "helpers" / "__init__.py",
            "from .money import MoneyDecimalField\n",
        )
        # Abstract model base AND plain mixin outside ``models.py``.
        _write(
            pkg / "common" / "bases.py",
            "from django.db import models\n"
            "from acme.helpers import MoneyDecimalField\n"
            "from ..base.fields import I18nCharField\n\n\n"
            "class PricedBase(models.Model):\n"
            "    base_price = MoneyDecimalField(max_digits=8, decimal_places=2)\n\n"
            "    class Meta:\n"
            "        abstract = True\n\n\n"
            "class LocalizedMixin(object):\n"
            "    localized_note = I18nCharField()\n\n"
            "    class Meta:\n"
            "        abstract = True\n",
        )
        _write(
            pkg / "catalog" / "local_fields.py",
            "from ..helpers import MoneyDecimalField\n\n\n"
            "class TaxRateField(MoneyDecimalField):\n"
            "    pass\n",
        )
        _write(
            pkg / "catalog" / "models.py",
            "from django.db import models\n"
            "from acme.base import fields as base_fields\n"
            "from acme.base.fields import (\n"
            "    PrimaryOneToOneField,\n"
            "    ScopedForeignKey as OrgFK,\n"
            ")\n"
            "from acme.common.bases import LocalizedMixin, PricedBase\n"
            "from .local_fields import TaxRateField\n\n\n"
            "class Organization(models.Model):\n"
            "    slug = models.SlugField()\n\n\n"
            "class Voucher(LocalizedMixin, PricedBase):\n"
            '    org = OrgFK("Organization", on_delete=models.PROTECT, related_name="vouchers")\n'
            '    parent_voucher = PrimaryOneToOneField("self", on_delete=models.SET_NULL, null=True)\n'
            "    title = base_fields.I18nCharField()\n"
            "    tax_rate = TaxRateField(max_digits=5, decimal_places=2)\n",
        )

    def test_resolves_chains_reexports_relations_and_abstract_bases(self) -> None:
        index = scan_workspace(str(self.root))
        models = {m.name: m for app in index.apps for m in app.models}
        self.assertEqual(sorted(models), ["Organization", "Voucher"])

        voucher = models["Voucher"]
        self.assertEqual(
            [(f.name, f.type) for f in voucher.fields],
            [
                ("org", "ForeignKey"),
                ("parent_voucher", "OneToOneField"),
                ("title", "Field"),
                ("tax_rate", "DecimalField"),
            ],
        )

        org = next(f for f in voucher.fields if f.name == "org")
        self.assertTrue(org.is_relation)
        self.assertEqual(org.relation_kind, "ForeignKey")
        self.assertEqual(org.related_model, "Organization")
        self.assertEqual(org.on_delete, "PROTECT")
        self.assertEqual(org.related_name, "vouchers")

        parent_v = next(f for f in voucher.fields if f.name == "parent_voucher")
        self.assertTrue(parent_v.is_relation)
        self.assertEqual(parent_v.relation_kind, "OneToOneField")
        self.assertEqual(parent_v.related_model, "self")
        self.assertEqual(parent_v.on_delete, "SET_NULL")

        self.assertEqual(
            [(f.name, f.type, f.inherited_from) for f in voucher.inherited_fields],
            [
                ("localized_note", "Field", "LocalizedMixin"),
                ("base_price", "DecimalField", "PricedBase"),
            ],
        )

    def test_resolves_reexported_module_bindings_in_package_init(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(
                root / "core" / "__init__.py",
                "from . import fields as exported_fields\nfrom . import bad_cycle as bad_cycle\n",
            )
            _write(
                root / "core" / "fields.py",
                "from django.db import models\n\n\n"
                "class MoneyField(models.DecimalField):\n"
                "    pass\n",
            )
            _write(
                root / "shop" / "models.py",
                "import core\n"
                "from django.db import models\n"
                "from core import bad_cycle as cyc\n"
                "from core import exported_fields as f\n\n\n"
                "class Invoice(models.Model):\n"
                "    price = f.MoneyField(max_digits=10, decimal_places=2)\n"
                "    fee = core.exported_fields.MoneyField(max_digits=6, decimal_places=2)\n"
                "    ignored = cyc.MissingField()\n",
            )
            index = scan_workspace(str(root))
            models = {m.name: m for app in index.apps for m in app.models}
            self.assertEqual(
                [(f.name, f.type) for f in models["Invoice"].fields],
                [("price", "DecimalField"), ("fee", "DecimalField")],
            )


class NonFieldGuardsTest(unittest.TestCase):
    """Non-field calls, managers, form/serializer fields, and cycles stay ignored."""

    def test_ignores_helpers_managers_form_fields_and_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(
                root / "core" / "helpers.py",
                "from django import forms\n"
                "from django.db import models\n"
                "from rest_framework import serializers\n\n\n"
                "def make_default():\n"
                '    return "x"\n\n\n'
                "class FormCodeField(forms.CharField):\n"
                "    pass\n\n\n"
                "class SerializerCodeField(serializers.CharField):\n"
                "    pass\n\n\n"
                "class ActiveManager(models.Manager):\n"
                "    pass\n\n\n"
                "class CycleA(CycleB):\n"  # noqa: F821
                "    pass\n\n\n"
                "class CycleB(CycleA):\n"
                "    pass\n",
            )
            _write(
                root / "shop" / "models.py",
                "from django.db import models\n"
                "from core.helpers import (\n"
                "    ActiveManager,\n"
                "    CycleA,\n"
                "    FormCodeField,\n"
                "    SerializerCodeField,\n"
                "    make_default,\n"
                ")\n\n\n"
                "class Status(models.TextChoices):\n"
                '    OPEN = "open", "Open"\n\n\n'
                "class Order(models.Model):\n"
                "    code = models.CharField(max_length=32)\n"
                "    computed = make_default()\n"
                "    form_only = FormCodeField()\n"
                "    serializer_only = SerializerCodeField()\n"
                "    objects = ActiveManager()\n"
                "    broken = CycleA()\n",
            )
            index = scan_workspace(str(root))
            models = {m.name: m for app in index.apps for m in app.models}
            self.assertEqual(list(models), ["Order"])
            self.assertEqual(
                [(f.name, f.type) for f in models["Order"].fields],
                [("code", "CharField")],
            )

    def test_postgres_and_third_party_model_fields_accepted_but_forms_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(
                root / "core" / "fields.py",
                "from django.contrib.postgres.fields import ArrayField\n"
                "from django.contrib.postgres.forms import SimpleArrayField\n"
                "from django.contrib.postgres.search import SearchVectorField\n"
                "from mptt.fields import TreeForeignKey\n"
                "from mptt.forms import TreeNodeChoiceField\n"
                "from taggit.forms import TagField\n"
                "from taggit.managers import TaggableManager\n\n\n"
                "class TagListField(ArrayField):\n"
                "    pass\n\n\n"
                "class FullTextField(SearchVectorField):\n"
                "    pass\n\n\n"
                "class CategoryTreeFK(TreeForeignKey):\n"
                "    pass\n\n\n"
                "class CustomTaggableManager(TaggableManager):\n"
                "    pass\n\n\n"
                "class FormArrayField(SimpleArrayField):\n"
                "    pass\n\n\n"
                "class FormTreeChoiceField(TreeNodeChoiceField):\n"
                "    pass\n\n\n"
                "class FormTagField(TagField):\n"
                "    pass\n",
            )
            _write(
                root / "blog" / "models.py",
                "from django.db import models\n"
                "from core.fields import (\n"
                "    CategoryTreeFK,\n"
                "    CustomTaggableManager,\n"
                "    FormArrayField,\n"
                "    FormTagField,\n"
                "    FormTreeChoiceField,\n"
                "    FullTextField,\n"
                "    TagListField,\n"
                ")\n\n\n"
                "class Article(models.Model):\n"
                "    labels = TagListField(models.CharField(max_length=20))\n"
                "    search = FullTextField()\n"
                '    parent = CategoryTreeFK("self", on_delete=models.CASCADE, null=True)\n'
                "    tags = CustomTaggableManager()\n"
                "    form_arr = FormArrayField(models.CharField(max_length=20))\n"
                "    form_tree = FormTreeChoiceField(queryset=None)\n"
                "    form_tag = FormTagField()\n",
            )
            index = scan_workspace(str(root))
            models = {m.name: m for app in index.apps for m in app.models}
            article = models["Article"]
            self.assertEqual(
                [(f.name, f.type) for f in article.fields],
                [
                    ("labels", "ArrayField"),
                    ("search", "SearchVectorField"),
                    ("parent", "TreeForeignKey"),
                    ("tags", "TaggableManager"),
                ],
            )
            parent = next(f for f in article.fields if f.name == "parent")
            self.assertEqual(parent.relation_kind, "ForeignKey")
            tags = next(f for f in article.fields if f.name == "tags")
            self.assertEqual(tags.relation_kind, "ManyToManyField")


if __name__ == "__main__":
    unittest.main()
