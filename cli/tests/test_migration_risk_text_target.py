"""Regression tests for the ``migration-risk`` text header target.

The text format used to print ``f"{f.app}.{f.model}"`` unconditionally, so
graph-level findings (``conflicting_migration_leaves``,
``alembic_multiple_heads``), which carry ``model=None``, rendered as
``(migration graph) shop.None``. The header now joins only the parts that
are set.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

from orm_lens.cli import _format_risk_target, main
from orm_lens.migrations_parser import MigrationRisk

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "migration_risks"
CONFLICTING_LEAVES = FIXTURE_ROOT / "conflicting_leaves"
ADD_FIELD_NOTNULL = FIXTURE_ROOT / "add_field_notnull"


def _run(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(argv)
    return code, out.getvalue()


def _finding(*, app: str, model: str | None, field: str | None) -> MigrationRisk:
    return MigrationRisk(
        file_path="shop/migrations/0001_initial.py",
        line_number=1,
        app=app,
        migration="0001_initial",
        operation="(migration graph)",
        model=model,
        field=field,
        rule="conflicting_migration_leaves",
        description="",
        mitigation="",
        confidence="high",
        severity="critical",
    )


def test_target_omits_missing_model() -> None:
    finding = _finding(app="shop", model=None, field=None)
    assert _format_risk_target(finding) == "shop"


def test_target_omits_missing_field() -> None:
    finding = _finding(app="shop", model="item", field=None)
    assert _format_risk_target(finding) == "shop.item"


def test_target_includes_field_when_set() -> None:
    finding = _finding(app="shop", model="item", field="price")
    assert _format_risk_target(finding) == "shop.item.price"


def test_text_output_prints_app_for_graph_findings() -> None:
    code, out = _run(
        [
            "migration-risk",
            "--path",
            str(CONFLICTING_LEAVES),
            "--exit-zero",
        ]
    )

    assert code == 0
    assert "(migration graph) shop" in out
    assert "shop.None" not in out


def test_text_output_keeps_app_model_field_for_operation_findings() -> None:
    code, out = _run(
        [
            "migration-risk",
            "--path",
            str(ADD_FIELD_NOTNULL),
            "--exit-zero",
        ]
    )

    assert code == 0
    assert "add_field_notnull.order.status" in out
