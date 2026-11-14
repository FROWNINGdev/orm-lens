"""Regression tests for issue #249 — workspace Mixins are not models by name.

``_looks_like_model`` used to treat any base whose name ends in ``Mixin`` as
a Django model. That guess exists for third-party bases the scanner cannot
see, but it also fired for plain classes defined in the workspace — a
``QuerySet`` mixin or a URL helper — so ``drift`` reported them as declared
models with no migration.

A base defined in the analysed workspace must be resolved instead of guessed:
only a chain that reaches a real Django model base makes a class a model.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orm_lens.parser import parse_models_file, scan_workspace


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


LABEL_STUDIO_MIXINS = """
class StateQuerySetMixin:
    def with_state(self):
        return self
"""

LABEL_STUDIO_MODELS = """
from django.db import models

from .mixins import StateQuerySetMixin


class ProjectQuerySet(models.QuerySet):
    pass


class ProjectQuerySetWithState(StateQuerySetMixin, ProjectQuerySet):
    pass


class Project(models.Model):
    title = models.CharField(max_length=100)

    objects = ProjectQuerySetWithState.as_manager()
"""

WEBLATE_MIXINS = """
class BaseURLMixin:
    def base_url(self):
        return "/"


class URLMixin(BaseURLMixin):
    def get_absolute_url(self):
        return "/"


class LoggerMixin(BaseURLMixin):
    def log(self):
        pass


class PathMixin(LoggerMixin, URLMixin):
    def full_path(self):
        return self.get_absolute_url()
"""

WEBLATE_MODELS = """
from django.db import models

from .mixins import PathMixin


class Project(models.Model, PathMixin):
    title = models.CharField(max_length=100)
"""


class PlainMixinIsNotAModelTest(unittest.TestCase):
    def _models(self, app: str, mixins: str, models: str) -> set[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / app / "mixins.py", mixins)
            _write(root / app / "models.py", models)
            index = scan_workspace(str(root))
            return {m.name for a in index.apps for m in a.models}

    def test_queryset_mixin_is_not_a_model(self) -> None:
        # label-studio: ProjectQuerySetWithState(StateQuerySetMixin,
        # ProjectQuerySet), where StateQuerySetMixin is a plain workspace
        # class and ProjectQuerySet is a models.QuerySet.
        self.assertEqual(
            self._models("projects", LABEL_STUDIO_MIXINS, LABEL_STUDIO_MODELS),
            {"Project"},
        )

    def test_url_helper_mixins_are_not_models(self) -> None:
        # weblate: PathMixin is pulled in because Project inherits it, and
        # its ancestors (all plain helpers) must not become models with it.
        self.assertEqual(
            self._models("trans", WEBLATE_MIXINS, WEBLATE_MODELS),
            {"Project"},
        )


class AbstractModelMixinStillRecognisedTest(unittest.TestCase):
    def test_abstract_model_mixin_still_makes_its_subclass_a_model(self) -> None:
        src = (
            "from django.db import models\n"
            "\n"
            "class OwnedMixin(models.Model):\n"
            "    owner = models.CharField(max_length=50)\n"
            "    class Meta:\n"
            "        abstract = True\n"
            "\n"
            "class Doc(OwnedMixin):\n"
            "    title = models.CharField(max_length=100)\n"
        )
        models = parse_models_file("docs/models.py", src)
        self.assertEqual([m.name for m in models], ["Doc"])
        self.assertEqual([f.name for f in models[0].inherited_fields], ["owner"])


if __name__ == "__main__":
    unittest.main()
