from __future__ import annotations

from scripts.ci.scope import (
    build_scope,
    dependents_closure,
    integration_shards,
    module_id_from_path,
    write_github_output,
)

GRAPH = {
    "mod_auth": [],
    "mod_catalog": [],
    "mod_cart": ["mod_catalog"],
    "mod_shipping": ["mod_cart"],
    "mod_checkout": ["mod_cart", "mod_shipping"],
    "mod_orders": ["mod_checkout"],
    "mod_postventa": ["mod_orders"],
    "mod_chat": [
        "mod_auth",
        "mod_catalog",
        "mod_cart",
        "mod_shipping",
        "mod_checkout",
        "mod_orders",
        "mod_postventa",
    ],
}


def test_module_id_from_module_and_test_paths() -> None:
    assert module_id_from_path("src/modules/mod_auth/domain/user.py") == "mod_auth"
    assert module_id_from_path("tests/unit/mod_catalog/test_search.py") == "mod_catalog"
    assert module_id_from_path("tests/integration/mod_chat/test_chat.py") == "mod_chat"
    assert module_id_from_path("src/shared/domain/x.py") is None
    assert module_id_from_path("docs/readme.md") is None


def test_dependents_closure_walks_reverse_edges() -> None:
    assert dependents_closure(["mod_catalog"], GRAPH) == [
        "mod_cart",
        "mod_catalog",
        "mod_chat",
        "mod_checkout",
        "mod_orders",
        "mod_postventa",
        "mod_shipping",
    ]
    assert dependents_closure(["mod_auth"], GRAPH) == ["mod_auth", "mod_chat"]


def test_docs_only_skips_code_jobs() -> None:
    scope = build_scope(
        ["README.md", "docs/adr/ADR-0001.md"],
        graph=GRAPH,
        event="pull_request",
        labels=[],
        full=False,
    )
    assert scope["mode"] == "docs"
    assert scope["run_validate"] is False
    assert scope["run_unit"] is False
    assert scope["run_integration"] is False
    assert scope["run_sonar"] is False
    assert scope["integration_shards"] == []


def test_shared_code_forces_full_scope() -> None:
    scope = build_scope(
        ["src/shared/domain/module_errors.py"],
        graph=GRAPH,
        event="pull_request",
        labels=[],
        full=False,
    )
    assert scope["mode"] == "code"
    assert scope["full"] is True
    assert scope["run_unit"] is True
    assert scope["run_integration"] is True
    assert scope["integration_shards"][-1] == "core"
    assert set(scope["integration_shards"]) >= {
        "mod_catalog",
        "mod_chat",
        "mod_checkout",
        "mod_orders",
    }


def test_dependency_change_runs_audit_and_full_suite() -> None:
    scope = build_scope(
        ["requirements.txt"],
        graph=GRAPH,
        event="pull_request",
        labels=[],
        full=False,
    )
    assert scope["mode"] == "deps"
    assert scope["full"] is True
    assert scope["run_security"] is True
    assert scope["run_unit"] is True


def test_changed_module_selects_integration_shards() -> None:
    scope = build_scope(
        ["src/modules/mod_catalog/application/search.py"],
        graph=GRAPH,
        event="pull_request",
        labels=[],
        full=False,
    )
    assert scope["run_integration"] is True
    assert scope["integration_shards"] == [
        "mod_catalog",
        "mod_chat",
        "mod_checkout",
        "mod_orders",
    ]


def test_root_integration_test_adds_core_shard() -> None:
    shards = integration_shards(
        ["mod_orders"],
        ["tests/integration/test_outbox_consumer_rabbitmq.py"],
        full_scope=False,
    )
    assert shards == ["mod_orders", "core"]


def test_expensive_jobs_only_run_on_push_or_label() -> None:
    pull_request = build_scope(
        ["src/modules/mod_catalog/application/search.py"],
        graph=GRAPH,
        event="pull_request",
        labels=[],
        full=False,
    )
    assert pull_request["run_dast"] is False
    assert pull_request["run_gateway"] is False

    labelled = build_scope(
        ["src/modules/mod_catalog/application/search.py"],
        graph=GRAPH,
        event="pull_request",
        labels=["ci:full"],
        full=False,
    )
    assert labelled["run_dast"] is True
    assert labelled["run_gateway"] is True


def test_forced_full_without_diff_runs_code_jobs() -> None:
    scope = build_scope(
        [],
        graph=GRAPH,
        event="workflow_dispatch",
        labels=[],
        full=True,
    )
    assert scope["mode"] == "code"
    assert scope["full"] is True
    assert scope["run_unit"] is True


def test_write_github_output_serializes_arrays(tmp_path) -> None:
    output = tmp_path / "github-output"
    write_github_output(output, {"modules": ["mod_a", "mod_b"], "run_unit": True})
    text = output.read_text(encoding="utf-8")
    assert 'modules=["mod_a", "mod_b"]' in text
    assert "run_unit=true" in text
