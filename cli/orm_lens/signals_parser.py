"""Static Django signal graph extractor — pure AST, no Django boot, no DB.

Walks every ``.py`` file in the workspace and extracts three things:

1. Every ``@receiver(signal[, sender=X])`` decorated function and every
   ``signal.connect(handler[, sender=X])`` call — captured as a
   ``sender → signal → handler`` edge. ``sender`` is normalised to
   ``app.Model`` when the class name resolves against the parsed workspace,
   or left as the raw source token when it doesn't (with a note).
2. Every ``foo = Signal(...)`` module-level assignment — captured as a
   custom signal definition with fully-qualified name.
3. Every ``<custom_signal>.send(...)`` / ``.send_robust(...)`` call — used
   to populate the ``sent_from`` list on each custom signal definition.

A separate ``orphan_handlers`` bucket lists receivers whose declared
``sender`` couldn't be resolved to any model in the workspace — a common
smell (typo, string reference to an app not imported, obsolete signal).

Directories skipped: ``migrations/``, ``node_modules/``, ``venv/``, ``env/``,
``.git/``, ``__pycache__/`` and any dot-prefixed folder.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from .models import WorkspaceIndex, find_user_model

# Only model signals can fire for all models when no sender is specified.
_MODEL_SIGNALS = frozenset(
    {
        "pre_init", "post_init",
        "pre_save", "post_save",
        "pre_delete", "post_delete",
        "m2m_changed",
    }
)

# Django built-in signals we know about — used for tagging edges as "django-native".
_BUILTIN_SIGNALS = _MODEL_SIGNALS | frozenset(
    {
        "pre_migrate", "post_migrate",
        "request_started", "request_finished", "got_request_exception",
        "setting_changed",
        "user_logged_in", "user_logged_out", "user_login_failed",
    }
)


def _rel(root: Path, path: Path, line: int) -> str:
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = path.as_posix()
    return f"{rel}:{line}"


def _module_path_from_file(root: Path, path: Path) -> str:
    """``root/notifications/signals.py`` → ``notifications.signals``."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    parts = list(rel.parts)
    if parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _name_of(node: ast.AST) -> str | None:
    """Best-effort textual dotted name of a Name/Attribute AST node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name_of(node.value)
        if base is None:
            return node.attr
        return f"{base}.{node.attr}"
    return None


def _collect_receiver_aliases(tree: ast.AST) -> set[str]:
    """Collect dotted names and bare identifiers in a module that resolve
    to ``django.dispatch.receiver``.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("django.dispatch", "django.dispatch.dispatcher"):
                    bound = alias.asname or alias.name
                    aliases.add(f"{bound}.receiver")
                    if not alias.asname:
                        aliases.add("dispatch.receiver")
                elif alias.name == "django":
                    bound = alias.asname or alias.name
                    aliases.add(f"{bound}.dispatch.receiver")
        elif isinstance(node, ast.ImportFrom):
            if not node.module:
                continue
            if node.module in ("django.dispatch", "django.dispatch.dispatcher"):
                for alias in node.names:
                    if alias.name == "receiver":
                        aliases.add(alias.asname or alias.name)
                    elif alias.name == "*":
                        aliases.add("receiver")
            elif node.module == "django":
                for alias in node.names:
                    if alias.name == "dispatch":
                        bound = alias.asname or alias.name
                        aliases.add(f"{bound}.receiver")
                    elif alias.name == "*":
                        aliases.add("dispatch.receiver")
    return aliases


_DJANGO_MODEL_SIGNALS_MODULE = "django.db.models.signals"


def _import_target(
    node: ast.ImportFrom, module_path: str, is_package: bool
) -> str | None:
    """Dotted module a ``from ... import`` reads from, or None if unreadable."""
    if node.level == 0:
        return node.module
    package = module_path if is_package else module_path.rpartition(".")[0]
    parts = package.split(".") if package else []
    if node.level - 1 > len(parts):
        return None
    parts = parts[: len(parts) - (node.level - 1)]
    if node.module:
        parts.append(node.module)
    return ".".join(parts)


def _collect_signal_origins(
    tree: ast.Module,
    module_path: str,
    is_package: bool,
    signal_defs_by_module: dict[str, dict[str, str]],
) -> dict[str, str | None]:
    """Map each name bound at module level to where the object came from.

    The value is the dotted path the name was imported from (so
    ``from django.db.models.signals import post_save as saved`` maps
    ``saved`` to ``django.db.models.signals.post_save``), or ``None`` for a
    name bound locally: assigned, defined, or imported from an unreadable path.
    Relative imports are resolved to the workspace module they name. Later
    bindings replace earlier ones, as they do at runtime, so a ``Signal()``
    called ``post_save`` is told apart from Django's. Names that are not
    bound at module level are absent; the caller falls back to the signal's
    spelling for those.
    """
    origins: dict[str, str | None] = {}

    def resolve(name: str) -> str | None:
        head, _, rest = name.partition(".")
        base = origins.get(head)
        if base is None:
            return None
        return f"{base}.{rest}" if rest else base

    def bind_stores(target: ast.AST | None) -> None:
        if target is None:
            return
        for node in ast.walk(target):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                origins[node.id] = None

    def visit(stmts: list[ast.stmt]) -> None:
        for stmt in stmts:
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    if alias.asname:
                        origins[alias.asname] = alias.name
                    else:
                        head = alias.name.partition(".")[0]
                        origins[head] = head
            elif isinstance(stmt, ast.ImportFrom):
                module = _import_target(stmt, module_path, is_package)
                for alias in stmt.names:
                    if alias.name == "*":
                        if module == _DJANGO_MODEL_SIGNALS_MODULE:
                            for name in _MODEL_SIGNALS:
                                origins[name] = f"{module}.{name}"
                        elif module is not None:
                            for name in signal_defs_by_module.get(module, {}):
                                origins[name] = f"{module}.{name}"
                    elif module is None:
                        origins[alias.asname or alias.name] = None
                    else:
                        origins[alias.asname or alias.name] = f"{module}.{alias.name}"
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                origins[stmt.name] = None
            elif isinstance(stmt, ast.Assign):
                value = _name_of(stmt.value)
                target = stmt.targets[0]
                if len(stmt.targets) == 1 and isinstance(target, ast.Name) and value is not None:
                    origins[target.id] = resolve(value)
                else:
                    for target in stmt.targets:
                        bind_stores(target)
            elif isinstance(stmt, ast.AnnAssign):
                bind_stores(stmt.target)
            else:
                if isinstance(stmt, (ast.For, ast.AsyncFor)):
                    bind_stores(stmt.target)
                elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                    for item in stmt.items:
                        bind_stores(item.optional_vars)
                for field in ("body", "orelse", "finalbody"):
                    visit(getattr(stmt, field, []))
                for handler in getattr(stmt, "handlers", []):
                    if handler.name:
                        origins[handler.name] = None
                    visit(handler.body)

    visit(tree.body)
    return origins


def _classify_signal(
    signal_name: str, origins: dict[str, str | None]
) -> tuple[bool, bool]:
    """Return ``(is_model_signal, is_builtin)`` for a signal as written.

    A name or module bound at module level is judged by where it came from,
    so a custom ``post_save`` is neither. A name that is not bound there
    (a function-local import, or a star import from a module we cannot
    read) is judged by its spelling.
    """
    head, _, rest = signal_name.partition(".")
    tail = signal_name.rsplit(".", 1)[-1]
    if head not in origins:
        return tail in _MODEL_SIGNALS, tail in _BUILTIN_SIGNALS
    base = origins[head]
    if base is None:
        return False, False
    origin = f"{base}.{rest}" if rest else base
    origin_tail = origin.rsplit(".", 1)[-1]
    is_django = origin.startswith("django.")
    return (
        origin.startswith(_DJANGO_MODEL_SIGNALS_MODULE + ".") and origin_tail in _MODEL_SIGNALS,
        is_django and origin_tail in _BUILTIN_SIGNALS,
    )


def _extract_receiver_call(
    dec: ast.AST, receiver_aliases: set[str] | frozenset[str] | None = None
) -> ast.Call | None:
    """Return the ``receiver(...)`` Call node if ``dec`` is one, else None.

    Matches decorators whose dotted name resolves to ``django.dispatch.receiver``.
    When ``receiver_aliases`` is omitted, falls back to the canonical forms
    ``{"receiver", "dispatch.receiver", "django.dispatch.receiver"}``.
    """
    if not isinstance(dec, ast.Call):
        return None
    func_name = _name_of(dec.func)
    if not func_name:
        return None
    if receiver_aliases is None:
        receiver_aliases = {"receiver", "dispatch.receiver", "django.dispatch.receiver"}
    if func_name not in receiver_aliases:
        return None
    return dec


def _signal_arg_names(call: ast.Call) -> list[str]:
    """Signal names in the positional argument or ``signal=`` keyword.

    Django accepts one signal (``post_save``) or a list/tuple of them
    (``[post_save, post_delete]``). Unreadable elements are skipped; when
    none can be read, returns an empty list and the caller records
    ``<unknown>`` once.
    """
    first = call.args[0] if call.args else next(
        (kw.value for kw in call.keywords if kw.arg == "signal"), None
    )
    if first is None:
        return []
    nodes = first.elts if isinstance(first, (ast.List, ast.Tuple)) else [first]
    names = [_name_of(node) for node in nodes]
    return [name for name in names if name is not None]


def _sender_node(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return _name_of(node)


def _sender_kwarg(call: ast.Call) -> str | None:
    for kw in call.keywords:
        if kw.arg == "sender":
            return _sender_node(kw.value)
    return None


def _connect_call(
    call: ast.Call, known_signals: set[str]
) -> tuple[str, str, str | None] | None:
    """Read ``<signal>.connect(<handler>[, <sender> | sender=X])`` as
    (signal, handler, raw sender), or None if ``call`` is not one.

    ``.connect`` is a common method name (sockets, database clients), so the
    call only counts when the signal is a Django built-in or a ``Signal()``
    defined in the workspace, or when it passes ``sender=``.
    """
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr != "connect":
        return None
    signal_name = _name_of(func.value)
    if not signal_name:
        return None
    handler_node: ast.AST | None = call.args[0] if call.args else None
    for kw in call.keywords:
        if kw.arg == "receiver":
            handler_node = kw.value
    handler = _name_of(handler_node) if handler_node is not None else None
    if not handler:
        return None
    sender_kw = _sender_kwarg(call)
    tail = signal_name.rsplit(".", 1)[-1]
    if (
        tail not in _BUILTIN_SIGNALS
        and tail not in known_signals
        and sender_kw is None
    ):
        return None
    sender_raw = sender_kw
    if sender_raw is None and len(call.args) >= 2:
        sender_raw = _sender_node(call.args[1])
    return signal_name, handler, sender_raw


def _iter_signal_definitions(tree: ast.Module) -> list[tuple[str, int]]:
    """Yield (var_name, lineno) for every module-level ``x = Signal(...)``."""
    out: list[tuple[str, int]] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        func_name = _name_of(node.value.func)
        if not func_name:
            continue
        if func_name.rsplit(".", 1)[-1] != "Signal":
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                out.append((target.id, node.lineno))
    return out


def _resolve_sender(
    raw: str | None,
    model_index: dict[str, str],
    user_model_name: str | None = None,
) -> tuple[str | None, str | None]:
    """Turn a raw sender token into (canonical, note).

    * ``None`` → (None, None) → no sender filter.
    * ``"Order"`` → look up in model_index; return (``"orders.Order"``, None)
      or (raw, "sender model not found in workspace") on miss.
    * dotted / string forms (``"orders.Order"``, ``"MyApp.Model"``) →
      normalise via model_index tail lookup.
    * ``"settings.AUTH_USER_MODEL"`` / bare ``"AUTH_USER_MODEL"`` →
      resolve to the workspace's User model when known; otherwise
      surface as an orphan (helps flag misconfigured signal receivers).
    """
    if raw is None:
        return None, None
    tail = raw.rsplit(".", 1)[-1]
    if tail == "AUTH_USER_MODEL":
        if user_model_name:
            canonical = model_index.get(user_model_name)
            if canonical:
                return canonical, None
        return raw, "sender model not found in workspace"
    canonical = model_index.get(tail)
    if canonical:
        return canonical, None
    return raw, "sender model not found in workspace"


def _build_model_index(workspace: WorkspaceIndex) -> dict[str, str]:
    """``Order`` → ``orders.Order``. Last write wins on collisions (rare in
    real Django projects — model names are usually unique per project)."""
    out: dict[str, str] = {}
    for app in workspace.apps:
        for m in app.models:
            out[m.name] = f"{app.name}.{m.name}"
    return out


def signal_graph(
    root: str, index: WorkspaceIndex | None = None
) -> dict[str, Any]:
    """Return the sender→signal→handler graph for the workspace.

    Shape matches the MCP tool spec: ``edges``, ``custom_signals``,
    ``orphan_handlers``. Pure AST parse — no Django import, no DB.
    """
    from .parser import DEFAULT_EXCLUDES, scan_workspace

    if index is None:
        index = scan_workspace(root, DEFAULT_EXCLUDES)
    model_index = _build_model_index(index)
    user_model = find_user_model(index)
    user_model_name = user_model.name if user_model else None

    root_path = Path(root).resolve()

    edges: list[dict[str, Any]] = []
    orphan_handlers: list[dict[str, Any]] = []
    # custom_signal FQN → definition metadata
    custom_signals: dict[str, dict[str, Any]] = {}
    # short var name → FQN, per module (populated pass 1, consumed pass 2)
    signal_defs_by_module: dict[str, dict[str, str]] = {}

    from .parser import iter_workspace_py_files  # local to avoid cycles
    py_files: list[Path] = list(iter_workspace_py_files(root_path))
    parsed: list[tuple[Path, ast.Module, str]] = []

    # Pass 1 — collect Signal() definitions so pass 2 can resolve .send()
    # against them.
    for py in py_files:
        try:
            source = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=str(py))
        except SyntaxError:
            continue
        module_path = _module_path_from_file(root_path, py)
        parsed.append((py, tree, module_path))
        module_defs: dict[str, str] = {}
        for var_name, lineno in _iter_signal_definitions(tree):
            fqn = f"{module_path}.{var_name}" if module_path else var_name
            custom_signals[fqn] = {
                "name": var_name,
                "defined_in": _rel(root_path, py, lineno),
                "sent_from": [],
            }
            module_defs[var_name] = fqn
        if module_defs:
            signal_defs_by_module[module_path] = module_defs

    # Also build a bare-name → FQN map for cross-module short refs
    # (only used when a name is unambiguous across the workspace).
    bare_signal_map: dict[str, list[str]] = {}
    for fqn, meta in custom_signals.items():
        bare_signal_map.setdefault(meta["name"], []).append(fqn)
    known_signals = set(bare_signal_map)

    def add_edge(
        signal_name: str,
        sender_raw: str | None,
        handler: str,
        file: str,
        via: str,
        *,
        origins: dict[str, str | None],
    ) -> None:
        sender, note = _resolve_sender(sender_raw, model_index, user_model_name)
        edge: dict[str, Any] = {
            "signal": signal_name,
            "sender": sender,
            "handler": handler,
            "file": file,
        }
        is_model_signal, is_builtin = _classify_signal(signal_name, origins)
        if sender_raw is None and is_model_signal:
            edge["note"] = "no sender = fires for all models"
        elif note:
            edge["note"] = note
            orphan_handlers.append(
                {
                    "handler": handler,
                    "reason": f"{via} but sender model not found in workspace",
                }
            )
        if is_builtin:
            edge["builtin"] = True
        edges.append(edge)

    # Pass 2 — decorators, .connect() and .send() calls.
    for py, tree, module_path in parsed:
        receiver_aliases = _collect_receiver_aliases(tree)
        origins = _collect_signal_origins(
            tree, module_path, py.name == "__init__.py", signal_defs_by_module
        )
        # Walk all functions (module-level + nested) for @receiver decorators.
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    rcall = _extract_receiver_call(dec, receiver_aliases)
                    if rcall is None:
                        continue
                    handler_fqn = (
                        f"{module_path}.{node.name}" if module_path else node.name
                    )
                    signal_names = _signal_arg_names(rcall) or ["<unknown>"]
                    for signal_name in signal_names:
                        add_edge(
                            signal_name,
                            _sender_kwarg(rcall),
                            handler_fqn,
                            _rel(root_path, py, node.lineno),
                            "@receiver",
                            origins=origins,
                        )

            if isinstance(node, ast.Call):
                # <signal>.connect(handler, sender=...) — the handler is
                # recorded as written; it is often imported from elsewhere.
                connected = _connect_call(node, known_signals)
                if connected is not None:
                    signal_name, handler, sender_raw = connected
                    add_edge(
                        signal_name,
                        sender_raw,
                        handler,
                        _rel(root_path, py, node.lineno),
                        ".connect()",
                        origins=origins,
                    )
                    continue

                # Custom signal .send() / .send_robust() call collection.
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                if func.attr not in ("send", "send_robust"):
                    continue
                base_name = _name_of(func.value)
                if not base_name:
                    continue
                # Try local-module resolution first, then bare-name lookup.
                local_defs = signal_defs_by_module.get(module_path, {})
                sig_fqn = local_defs.get(base_name)
                if sig_fqn is None:
                    candidates = bare_signal_map.get(base_name.rsplit(".", 1)[-1], [])
                    if len(candidates) == 1:
                        sig_fqn = candidates[0]
                if sig_fqn is None:
                    continue
                custom_signals[sig_fqn]["sent_from"].append(
                    _rel(root_path, py, node.lineno)
                )

    # Dedup orphan_handlers on (handler,) — a receiver decorated twice for
    # different missing senders shouldn't spam the list.
    seen_orphans: set[str] = set()
    dedup_orphans: list[dict[str, Any]] = []
    for row in orphan_handlers:
        key = row["handler"]
        if key in seen_orphans:
            continue
        seen_orphans.add(key)
        dedup_orphans.append(row)

    return {
        "edges": edges,
        "custom_signals": [
            {"name": meta["name"], "fqn": fqn, **{k: v for k, v in meta.items() if k != "name"}}
            for fqn, meta in sorted(custom_signals.items())
        ],
        "orphan_handlers": dedup_orphans,
    }


_DELETE_SIGNALS = frozenset({"pre_delete", "post_delete"})


def skipped_delete_receivers(
    cascade: dict[str, Any], root: str, index: WorkspaceIndex | None = None
) -> dict[str, Any]:
    """Mark DB_CASCADE rows whose delete receivers will not run.

    Django 6.1's ``on_delete=DB_CASCADE`` deletes the child rows inside the
    database, so the ORM never collects them and sends no ``pre_delete`` /
    ``post_delete`` for them. A receiver registered for that model, or for
    every model, silently stops firing for those rows: audit logs, file
    cleanup and cache invalidation just stop. Adds ``skipped_receivers``
    (``signal``, ``handler``, ``file``) to each such entry of
    ``cascade["cascade_kills"]`` in place, and returns ``cascade``.

    Sees ``@receiver`` decorators and ``signal.connect(...)`` calls, the same
    scope as :func:`signal_graph`. The signal graph is only
    built when a ``DB_CASCADE`` relation exists, so plain schemas pay nothing.
    """
    targets = [e for e in cascade.get("cascade_kills", []) if e.get("db_level")]
    if not targets:
        return cascade
    edges = signal_graph(root, index)["edges"]
    for entry in targets:
        hits: list[dict[str, Any]] = []
        for edge in edges:
            signal = (edge.get("signal") or "").rsplit(".", 1)[-1]
            if signal not in _DELETE_SIGNALS:
                continue
            if edge.get("sender") not in (None, entry["model"]):
                continue
            hits.append(
                {"signal": signal, "handler": edge["handler"], "file": edge["file"]}
            )
        if hits:
            entry["skipped_receivers"] = hits
    return cascade
