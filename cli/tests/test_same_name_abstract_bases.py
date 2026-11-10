"""Regression test for issue #227 — abstract bases matched by name workspace-wide.

Two apps each define an ``AbstractLine`` base. Before the fix, every subclass
was matched to whichever ``AbstractLine`` was scanned first, so ``order.Line``
inherited the ``basket`` app's fields. A subclass must now inherit from the
base in its own directory.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orm_lens.parser import scan_workspace

BASKET_BASE = (
    "from django.db import models\n\n\n"
    "class AbstractLine(models.Model):\n"
    "    basket = models.CharField(max_length=50)\n\n"
    "    class Meta:\n"
    "        abstract = True\n"
)
ORDER_BASE = (
    "from django.db import models\n\n\n"
    "class AbstractLine(models.Model):\n"
    "    title = models.CharField(max_length=50)\n\n"
    "    class Meta:\n"
    "        abstract = True\n"
)
LINE_MODEL = (
    "from {app}.abstract_models import AbstractLine\n\n\n"
    "class Line(AbstractLine):\n"
    "    pass\n"
)


class SameNameAbstractBaseTest(unittest.TestCase):
    def test_subclass_inherits_from_own_app_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for app, base in (("basket", BASKET_BASE), ("order", ORDER_BASE)):
                (root / app).mkdir()
                (root / app / "abstract_models.py").write_text(base, encoding="utf-8")
                (root / app / "models.py").write_text(
                    LINE_MODEL.format(app=app), encoding="utf-8"
                )
            index = scan_workspace(str(root))
            lines = {
                a.name: next(m for m in a.models if m.name == "Line")
                for a in index.apps
            }
            self.assertEqual(
                sorted(f.name for f in lines["order"].all_fields()), ["title"]
            )
            self.assertEqual(
                sorted(f.name for f in lines["basket"].all_fields()), ["basket"]
            )


if __name__ == "__main__":
    unittest.main()
