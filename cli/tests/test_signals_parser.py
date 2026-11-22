"""Tests for the static signal-graph parser (signal_graph).

The parser walks every ``.py`` file in the workspace via pure ``ast``,
matches ``@receiver(...)`` decorators and ``x = Signal(...)`` module-level
assignments, and returns the sender→signal→handler DAG plus custom
signal definitions and their ``.send()`` call-sites.
"""

from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from orm_lens.signals_parser import signal_graph

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "miniapp"


def _copy_miniapp(dest: Path) -> None:
    for app in ("orders", "users"):
        shutil.copytree(FIXTURE_ROOT / app, dest / app)


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class SignalGraphTest(unittest.TestCase):
    def test_receiver_post_save_with_sender_resolves_to_model(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _copy_miniapp(root)
            _write(
                root / "notifications" / "signals.py",
                "from django.db.models.signals import post_save\n"
                "from django.dispatch import receiver\n"
                "from orders.models import Order\n\n"
                "@receiver(post_save, sender=Order)\n"
                "def notify_order_saved(sender, instance, **kwargs):\n"
                "    pass\n",
            )

            result = signal_graph(str(root))

            self.assertEqual(len(result["edges"]), 1)
            edge = result["edges"][0]
            self.assertEqual(edge["signal"], "post_save")
            self.assertEqual(edge["sender"], "orders.Order")
            self.assertTrue(edge["handler"].endswith("notify_order_saved"))
            self.assertIn("notifications", edge["handler"])
            self.assertTrue(edge.get("builtin"))
            self.assertEqual(result["orphan_handlers"], [])

    def test_receiver_no_sender_gets_null_and_note(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _copy_miniapp(root)
            _write(
                root / "audit" / "signals.py",
                "from django.db.models.signals import pre_delete\n"
                "from django.dispatch import receiver\n\n"
                "@receiver(pre_delete)\n"
                "def log_deletion(sender, instance, **kwargs):\n"
                "    pass\n",
            )

            result = signal_graph(str(root))

            self.assertEqual(len(result["edges"]), 1)
            edge = result["edges"][0]
            self.assertEqual(edge["signal"], "pre_delete")
            self.assertIsNone(edge["sender"])
            self.assertEqual(edge["note"], "no sender = fires for all models")

    def test_non_model_signals_without_sender_have_no_note(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root / "accounts" / "signals.py",
                "from django.contrib.auth.signals import user_logged_in\n"
                "from django.db.models import signals\n"
                "from django.dispatch import Signal, receiver\n\n"
                "order_shipped = Signal()\n\n"
                "@receiver(user_logged_in)\n"
                "def audit_event(sender, **kwargs):\n"
                "    pass\n\n"
                "signals.pre_migrate.connect(audit_event)\n"
                "order_shipped.connect(audit_event)\n",
            )
            result = signal_graph(str(root))

        builtin = {"user_logged_in": True, "signals.pre_migrate": True, "order_shipped": False}
        self.assertCountEqual([edge["signal"] for edge in result["edges"]], builtin)
        self.assertEqual(result["orphan_handlers"], [])
        for edge in result["edges"]:
            with self.subTest(signal=edge["signal"]):
                self.assertIsNone(edge["sender"])
                self.assertEqual(edge.get("builtin", False), builtin[edge["signal"]])
                self.assertNotIn("note", edge)

    def test_senderless_signal_aliases_use_the_imported_signal_for_the_note(self) -> None:
        cases = {
            "audit": (
                "from django.db.models.signals import post_save as saved",
                "saved", "no sender = fires for all models",
            ),
            "accounts": (
                "from django.contrib.auth.signals import user_logged_in as post_save",
                "post_save", None,
            ),
            "setup": (
                "from django.db.models.signals import pre_migrate as migrating",
                "migrating", None,
            ),
            "orders": ("from events import post_save as saved", "saved", None),
        }
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "events.py", "from django.dispatch import Signal\npost_save = Signal()\n")
            for app, (import_line, signal, _) in cases.items():
                _write(
                    root / app / "signals.py",
                    f"{import_line}\n"
                    "from django.dispatch import receiver\n\n"
                    f"@receiver({signal})\n"
                    "def audit(sender, **kwargs):\n"
                    "    pass\n",
                )
            result = signal_graph(str(root))

        edges = {edge["handler"]: edge for edge in result["edges"]}
        self.assertCountEqual(
            [edge["handler"] for edge in result["edges"]], [f"{app}.signals.audit" for app in cases]
        )
        self.assertEqual(result["orphan_handlers"], [])
        for app, (_, signal, note) in cases.items():
            with self.subTest(app=app):
                edge = edges[f"{app}.signals.audit"]
                self.assertEqual(edge["signal"], signal)
                self.assertIsNone(edge["sender"])
                if note is None:
                    self.assertNotIn("note", edge)
                else:
                    self.assertEqual(edge.get("note"), note)

    def test_receiver_with_aliased_model_signal_keeps_explicit_senders(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _copy_miniapp(root)
            _write(
                root / "audit" / "signals.py",
                "from django.db.models.signals import post_save as saved\n"
                "from django.dispatch import receiver\n"
                "from orders.models import Order\n\n"
                "@receiver(saved, sender=Order)\n"
                "def log_order(sender, **kwargs):\n"
                "    pass\n\n"
                "@receiver(saved, sender='Ghost')\n"
                "def log_ghost(sender, **kwargs):\n"
                "    pass\n",
            )
            result = signal_graph(str(root))

        self.assertCountEqual([edge["signal"] for edge in result["edges"]], ["saved", "saved"])
        edges = {edge["sender"]: edge for edge in result["edges"]}
        self.assertEqual(set(edges), {"orders.Order", "Ghost"})
        self.assertNotIn("note", edges["orders.Order"])
        self.assertEqual(edges["Ghost"]["note"], "sender model not found in workspace")
        self.assertEqual([row["handler"] for row in result["orphan_handlers"]], ["audit.signals.log_ghost"])

    def test_custom_signal_named_like_a_model_signal_has_no_all_models_note(self) -> None:
        django_import = "from django.db.models.signals import post_save"
        cases = {
            # app: (signal as written, module source)
            "local": ("post_save", "from django.dispatch import Signal\npost_save = Signal()\n"),
            "annotated": ("post_save", "from django.dispatch import Signal\npost_save: Signal = Signal()\n"),
            "imported": ("post_save", "from events import post_save\n"),
            "relative": ("post_save", "from .events import post_save\n"),
            "starred": ("post_save", "from events import *\n"),
            "module": ("events.post_save", "import events\n"),
            "package": ("events.post_save", "from . import events\n"),
            "rebound": ("post_save", f"{django_import}\nfrom events import post_save\n"),
            "copied": ("post_save", "from events import post_save as custom\npost_save = custom\n"),
        }
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "events.py", "from django.dispatch import Signal\npost_save = Signal()\n")
            for app, (signal, source) in cases.items():
                _write(root / app / "__init__.py", "")
                _write(root / app / "events.py", "from django.dispatch import Signal\npost_save = Signal()\n")
                _write(
                    root / app / "signals.py",
                    f"{source}from django.dispatch import receiver\n\n"
                    f"@receiver({signal})\n"
                    "def audit(sender, **kwargs):\n"
                    "    pass\n\n"
                    f"{signal}.connect(audit)\n",
                )
            result = signal_graph(str(root))

        self.assertEqual(result["orphan_handlers"], [])
        self.assertEqual(len(result["edges"]), 2 * len(cases))
        for edge in result["edges"]:
            with self.subTest(handler=edge["handler"], file=edge["file"]):
                self.assertIsNone(edge["sender"])
                self.assertNotIn("note", edge)
                self.assertNotIn("builtin", edge)

    def test_relative_star_imports_resolve_custom_model_named_signals(self) -> None:
        def events(name: str) -> str:
            return f"from django.dispatch import Signal\n{name} = Signal()\n"

        # Each events module defines a different name, so a star import only
        # counts as custom when it resolves to the right module.
        sources = {
            "toplevel.py": ("from .events import *\n", "post_save"),
            "pkg/sub/signals.py": ("from .events import *\n", "post_delete"),
            "pkg/sub/deep.py": ("from ..events import *\n", "pre_save"),
            "pkg/__init__.py": ("from .events import *\n", "pre_save"),
        }
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "events.py", events("post_save"))
            _write(root / "pkg" / "events.py", events("pre_save"))
            _write(root / "pkg" / "sub" / "events.py", events("post_delete"))
            for rel, (star, signal) in sources.items():
                _write(
                    root / rel,
                    f"{star}from django.dispatch import receiver\n\n"
                    f"@receiver({signal})\n"
                    "def audit(sender, **kwargs):\n"
                    "    pass\n",
                )
            result = signal_graph(str(root))

        self.assertEqual(len(result["edges"]), len(sources))
        for edge in result["edges"]:
            with self.subTest(file=edge["file"]):
                self.assertNotIn("note", edge)
                self.assertNotIn("builtin", edge)

    def test_relative_imports_beyond_the_workspace_are_not_resolved(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "events.py", "from django.dispatch import Signal\npost_save = Signal()\n")
            _write(
                root / "pkg" / "starred.py",
                "from ...events import *\n"
                "from django.dispatch import receiver\n\n"
                "@receiver(post_save)\n"
                "def audit(sender, **kwargs):\n"
                "    pass\n",
            )
            _write(
                root / "pkg" / "renamed.py",
                "from .... import events as post_save\n"
                "from django.dispatch import receiver\n\n"
                "@receiver(post_save)\n"
                "def audit(sender, **kwargs):\n"
                "    pass\n",
            )
            result = signal_graph(str(root))

        edges = {edge["file"].split(":")[0]: edge for edge in result["edges"]}
        # An unreadable star import binds nothing, so the spelling decides.
        self.assertEqual(edges["pkg/starred.py"]["note"], "no sender = fires for all models")
        # An unreadable plain import still binds its name to something local.
        self.assertNotIn("note", edges["pkg/renamed.py"])

    def test_star_imports_bind_only_the_names_their_module_defines(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "events.py", "from django.dispatch import Signal\npost_save = Signal()\n")
            _write(
                root / "audit" / "signals.py",
                "from django.db.models.signals import *\n"
                "from events import *\n"
                "from django.dispatch import receiver\n\n"
                "@receiver(post_save)\n"
                "def on_custom(sender, **kwargs):\n"
                "    pass\n\n"
                "@receiver(pre_save)\n"
                "def on_django(sender, **kwargs):\n"
                "    pass\n",
            )
            result = signal_graph(str(root))

        edges = {edge["handler"]: edge for edge in result["edges"]}
        custom, django = edges["audit.signals.on_custom"], edges["audit.signals.on_django"]
        self.assertNotIn("note", custom)
        self.assertNotIn("builtin", custom)
        self.assertEqual(django["note"], "no sender = fires for all models")
        self.assertTrue(django["builtin"])

    def test_imported_model_signals_keep_the_all_models_note(self) -> None:
        cases = {
            "plain": ("from django.db.models.signals import post_save\n", "post_save"),
            "module": ("from django.db.models import signals\n", "signals.post_save"),
            "dotted": ("from django.db import models\n", "models.signals.post_save"),
            "package": ("import django.db.models.signals\n", "django.db.models.signals.post_save"),
            "renamed": ("import django.db.models.signals as sg\n", "sg.post_save"),
            "starred": ("from django.db.models.signals import *\n", "post_save"),
            "unbound": ("", "post_save"),
            "copied": (
                "from django.db.models.signals import post_save as saved\npost_save = saved\n",
                "post_save",
            ),
            "restored": (
                "from django.dispatch import Signal\npost_save = Signal()\n"
                "from django.db.models.signals import post_save\n",
                "post_save",
            ),
        }
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            # A custom ``post_save`` elsewhere must not change other modules.
            _write(root / "events.py", "from django.dispatch import Signal\npost_save = Signal()\n")
            for app, (source, signal) in cases.items():
                _write(
                    root / app / "signals.py",
                    f"{source}from django.dispatch import receiver\n\n"
                    f"@receiver({signal})\n"
                    "def audit(sender, **kwargs):\n"
                    "    pass\n\n"
                    f"{signal}.connect(audit)\n",
                )
            result = signal_graph(str(root))

        self.assertEqual(len(result["edges"]), 2 * len(cases))
        for edge in result["edges"]:
            with self.subTest(handler=edge["handler"], file=edge["file"]):
                self.assertIsNone(edge["sender"])
                self.assertEqual(edge["note"], "no sender = fires for all models")
                self.assertTrue(edge["builtin"])

    def test_custom_signal_named_like_a_model_signal_keeps_explicit_senders(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _copy_miniapp(root)
            _write(root / "events.py", "from django.dispatch import Signal\npost_save = Signal()\n")
            _write(
                root / "audit" / "signals.py",
                "from django.dispatch import receiver\n"
                "from events import post_save\n"
                "from orders.models import Order\n\n"
                "@receiver(post_save, sender=Order)\n"
                "def log_order(sender, **kwargs):\n"
                "    pass\n\n"
                "@receiver(post_save, sender='Ghost')\n"
                "def log_ghost(sender, **kwargs):\n"
                "    pass\n\n"
                "post_save.connect(log_order, Order)\n",
            )
            result = signal_graph(str(root))

        by_handler = {(e["handler"], e["sender"]): e for e in result["edges"]}
        self.assertEqual(
            set(by_handler),
            {
                ("audit.signals.log_order", "orders.Order"),
                ("audit.signals.log_ghost", "Ghost"),
                ("log_order", "orders.Order"),
            },
        )
        for key in (("audit.signals.log_order", "orders.Order"), ("log_order", "orders.Order")):
            self.assertNotIn("note", by_handler[key])
        self.assertEqual(
            by_handler[("audit.signals.log_ghost", "Ghost")]["note"],
            "sender model not found in workspace",
        )
        self.assertEqual(
            [row["handler"] for row in result["orphan_handlers"]], ["audit.signals.log_ghost"]
        )
        for edge in result["edges"]:
            self.assertNotIn("builtin", edge)

    def test_orphan_handler_when_sender_missing_from_workspace(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _copy_miniapp(root)
            _write(
                root / "notifications" / "signals.py",
                "from django.db.models.signals import post_save\n"
                "from django.dispatch import receiver\n\n"
                "@receiver(post_save, sender=Ghost)\n"
                "def unused_handler(sender, instance, **kwargs):\n"
                "    pass\n",
            )

            result = signal_graph(str(root))

            self.assertEqual(len(result["edges"]), 1)
            edge = result["edges"][0]
            self.assertEqual(edge["sender"], "Ghost")
            self.assertEqual(edge["note"], "sender model not found in workspace")
            self.assertEqual(len(result["orphan_handlers"]), 1)
            self.assertIn("unused_handler", result["orphan_handlers"][0]["handler"])

    def test_custom_signal_definition_and_send(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _copy_miniapp(root)
            _write(
                root / "orders" / "signals.py",
                "from django.dispatch import Signal\n\n"
                "order_shipped = Signal()\n",
            )
            _write(
                root / "orders" / "services.py",
                "from orders.signals import order_shipped\n\n"
                "def ship(order):\n"
                "    order_shipped.send(sender=order.__class__, order=order)\n",
            )

            result = signal_graph(str(root))

            self.assertEqual(len(result["custom_signals"]), 1)
            sig = result["custom_signals"][0]
            self.assertEqual(sig["name"], "order_shipped")
            self.assertIn("orders.signals", sig["fqn"])
            self.assertTrue(sig["defined_in"].endswith("signals.py:3"))
            self.assertEqual(len(sig["sent_from"]), 1)
            self.assertTrue(sig["sent_from"][0].endswith("services.py:4"))

    def test_receiver_string_sender_resolves(self) -> None:
        """``sender="Order"`` string reference also resolves via the model index."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _copy_miniapp(root)
            _write(
                root / "notifications" / "signals.py",
                "from django.db.models.signals import post_save\n"
                "from django.dispatch import receiver\n\n"
                "@receiver(post_save, sender='Order')\n"
                "def h(sender, instance, **kwargs):\n"
                "    pass\n",
            )
            result = signal_graph(str(root))
            self.assertEqual(result["edges"][0]["sender"], "orders.Order")

    def test_receiver_settings_auth_user_sender_resolves(self) -> None:
        """End-to-end regression: ``sender=settings.AUTH_USER_MODEL`` must
        resolve to the workspace's User model rather than orphan out.

        The miniapp fixture ships a ``users.User`` model that falls under
        the "model literally named User" branch of :func:`find_user_model`,
        so this exercises the whole ``signal_graph`` pipeline.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _copy_miniapp(root)
            _write(
                root / "notifications" / "signals.py",
                "from django.conf import settings\n"
                "from django.db.models.signals import post_save\n"
                "from django.dispatch import receiver\n\n"
                "@receiver(post_save, sender=settings.AUTH_USER_MODEL)\n"
                "def on_user_saved(sender, instance, **kwargs):\n"
                "    pass\n",
            )
            result = signal_graph(str(root))

            self.assertEqual(len(result["edges"]), 1)
            edge = result["edges"][0]
            self.assertEqual(edge["sender"], "users.User")
            self.assertNotIn("note", edge)
            self.assertEqual(result["orphan_handlers"], [])


class SignalConnectTest(unittest.TestCase):
    """``signal.connect(handler, sender=Model)`` registers a receiver just
    like ``@receiver`` does (issue #183)."""

    def _edges(self, body: str) -> list[dict]:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _copy_miniapp(root)
            _write(root / "orders" / "apps.py", body)
            return signal_graph(str(root))["edges"]

    def test_connect_with_sender(self) -> None:
        edges = self._edges(
            "from django.db.models.signals import post_delete\n"
            "from orders.handlers import delete_file_from_storage\n"
            "from orders.models import Order\n\n"
            "def ready():\n"
            "    post_delete.connect(delete_file_from_storage, sender=Order)\n"
        )
        self.assertEqual(
            edges,
            [
                {
                    "signal": "post_delete",
                    "sender": "orders.Order",
                    "handler": "delete_file_from_storage",
                    "file": "orders/apps.py:6",
                    "builtin": True,
                }
            ],
        )

    def test_connect_without_sender_fires_for_all_models(self) -> None:
        edges = self._edges(
            "from django.db.models.signals import post_delete\n\n"
            "post_delete.connect(handlers.audit)\n"
        )
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["handler"], "handlers.audit")
        self.assertIsNone(edges[0]["sender"])
        self.assertEqual(edges[0]["note"], "no sender = fires for all models")

    def test_connect_on_dotted_signal(self) -> None:
        edges = self._edges(
            "from django.db import models\n\n"
            "models.signals.post_delete.connect(receiver=cleanup, sender='Order')\n"
        )
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["signal"], "models.signals.post_delete")
        self.assertEqual(edges[0]["handler"], "cleanup")
        self.assertEqual(edges[0]["sender"], "orders.Order")

    def test_dotted_model_signal_without_sender_keeps_note(self) -> None:
        edges = self._edges(
            "from django.db import models\n\n"
            "models.signals.post_save.connect(handlers.audit)\n"
        )
        self.assertEqual(len(edges), 1)
        edge = edges[0]
        self.assertEqual(edge["signal"], "models.signals.post_save")
        self.assertIsNone(edge["sender"])
        self.assertTrue(edge["builtin"])
        self.assertEqual(edge["note"], "no sender = fires for all models")

    def test_connect_with_positional_sender(self) -> None:
        edges = self._edges(
            "from django.db.models.signals import post_save, pre_save\n"
            "from orders.handlers import delete_file_from_storage\n"
            "from orders.models import Order\n\n"
            "def ready():\n"
            "    pre_save.connect(delete_file_from_storage, Order)\n"
            "    post_save.connect(delete_file_from_storage, 'Order')\n"
        )
        self.assertEqual(len(edges), 2)
        self.assertEqual(edges[0]["sender"], "orders.Order")
        self.assertNotIn("note", edges[0])
        self.assertEqual(edges[1]["sender"], "orders.Order")
        self.assertNotIn("note", edges[1])

    def test_unrelated_connect_calls_are_ignored(self) -> None:
        edges = self._edges(
            "import socket\n\n"
            "sock = socket.socket()\n"
            "sock.connect(address)\n"
            "some_socket.connect(host, port)\n"
            "db.connect(dsn)\n"
        )
        self.assertEqual(edges, [])


if __name__ == "__main__":
    unittest.main()
