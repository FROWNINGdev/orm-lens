"""Regression test for issue #371 — a TestCase named like a model.

BookWyrm names its test classes after the models they test. The parser used
to keep every class whose *name* matched a model name, so a class named Book
inheriting from TestCase became a second, empty Book model. Test-case bases
are now in NON_MODEL_TAIL and such classes are dropped.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from orm_lens.parser import scan_workspace


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class TestCaseNamedLikeModelTest(unittest.TestCase):
    def test_testcase_named_like_model_is_not_a_model(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "shop" / "__init__.py", "")
            _write(
                root / "shop" / "models.py",
                "from django.db import models\n\n\n"
                "class Book(models.Model):\n"
                "    title = models.CharField(max_length=100)\n",
            )
            _write(root / "shop" / "tests" / "models" / "__init__.py", "")
            _write(
                root / "shop" / "tests" / "models" / "test_book.py",
                "from django.test import TestCase\n\n\n"
                "class Book(TestCase):\n"
                "    def test_title(self):\n"
                "        pass\n",
            )

            index = scan_workspace(str(root))
            books = [m for app in index.apps for m in app.models if m.name == "Book"]

            self.assertEqual(len(books), 1)
            self.assertEqual([f.name for f in books[0].fields], ["title"])

    def test_api_testcase_named_like_model_is_not_a_model(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "shop" / "__init__.py", "")
            _write(
                root / "shop" / "models.py",
                "from django.db import models\n\n\n"
                "class Book(models.Model):\n"
                "    title = models.CharField(max_length=100)\n",
            )
            _write(root / "shop" / "tests" / "models" / "__init__.py", "")
            _write(
                root / "shop" / "tests" / "models" / "test_book.py",
                "from rest_framework.test import APITestCase\n\n\n"
                "class Book(APITestCase):\n"
                "    def test_title(self):\n"
                "        pass\n",
            )

            index = scan_workspace(str(root))
            books = [m for app in index.apps for m in app.models if m.name == "Book"]

            self.assertEqual(len(books), 1)
            self.assertEqual([f.name for f in books[0].fields], ["title"])


if __name__ == "__main__":
    unittest.main()
