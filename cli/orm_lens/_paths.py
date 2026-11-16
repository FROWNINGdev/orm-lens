"""Path helpers shared by the Django and SQLAlchemy parsers.

Kept in a module of their own so that ``parser`` and ``sqlalchemy_parser``
need not import each other: ``parser`` calls into ``sqlalchemy_parser``, and
both walk the same directories and name apps the same way.
"""

from __future__ import annotations

import os

# Directory names skipped when walking a workspace for arbitrary ``.py`` files
# (n+1 scanner, signal graph). Kept broader than ``DEFAULT_EXCLUDES`` on purpose:
# those are glob patterns for the models.py walk, this is a dirname-based
# denylist for the whole-tree walk.
BROAD_SKIP_DIRS: frozenset = frozenset(
    {"migrations", "node_modules", "venv", ".venv", "env", ".git", "__pycache__"}
)


def _has_app_markers(d: str) -> bool:
    if not d:
        return False
    return os.path.isdir(os.path.join(d, "migrations")) or os.path.isfile(
        os.path.join(d, "apps.py")
    )


def _app_dir_for(fs_path: str) -> tuple[str, str]:
    parent = os.path.dirname(fs_path)
    parent_name = os.path.basename(parent)
    candidate = os.path.dirname(parent) if parent_name == "models" else parent

    if _has_app_markers(candidate):
        return candidate, os.path.basename(candidate)

    cur = candidate
    while True:
        parent_dir = os.path.dirname(cur)
        if not parent_dir or parent_dir == cur or not os.path.basename(parent_dir):
            break
        if not os.path.isfile(os.path.join(parent_dir, "__init__.py")):
            break
        cur = parent_dir
        if _has_app_markers(cur):
            return cur, os.path.basename(cur)

    return candidate, os.path.basename(candidate)
