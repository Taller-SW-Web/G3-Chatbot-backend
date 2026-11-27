from __future__ import annotations

import subprocess

from scripts.ci.scope import (
    _diff_files,
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


def test_ci_full_label_forces_everything_even_on_docs() -> None:
    scope = build_scope(
        ["README.md"],
        graph=GRAPH,
        event="pull_request",
        labels=["ci:full"],
        full=True,
    )
    assert scope["mode"] == "code"
    assert scope["run_validate"] is True
    assert scope["run_unit"] is True
    assert scope["run_integration"] is True
    assert scope["run_security"] is True
    assert scope["run_dast"] is True
    assert scope["run_gateway"] is True


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


def test_infra_only_skips_code_jobs() -> None:
    scope = build_scope(
        ["Dockerfile", ".dockerignore"],
        graph=GRAPH,
        event="pull_request",
        labels=[],
        full=False,
    )
    assert scope["mode"] == "infra"
    assert scope["run_docker"] is True
    assert scope["run_validate"] is False
    assert scope["run_sonar"] is False


def test_infra_plus_config_does_not_hide_code_gates() -> None:
    for extra in (
        "modules.manifest.json",
        "docs/openapi.json",
        "sonar-project.properties",
        "pytest.ini",
        "alembic.ini",
        "pyproject.toml",
    ):
        scope = build_scope(
            ["Dockerfile", extra],
            graph=GRAPH,
            event="pull_request",
            labels=[],
            full=False,
        )
        assert scope["mode"] == "code", extra
        assert scope["run_validate"] is True, extra
        assert scope["run_unit"] is True, extra
        assert scope["run_sonar"] is True, extra


def test_gateway_plus_sonar_config_does_not_hide_code_gates() -> None:
    scope = build_scope(
        ["gateway/kong.yml", "sonar-project.properties"],
        graph=GRAPH,
        event="pull_request",
        labels=[],
        full=False,
    )
    assert scope["mode"] == "code"
    assert scope["run_validate"] is True
    assert scope["run_sonar"] is True


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


def test_integration_shards_ignore_malicious_names_and_duplicate_core(tmp_path) -> None:
    root = tmp_path / "tests" / "integration"
    (root / "mod_chat").mkdir(parents=True)
    (root / "core").mkdir()
    (root / "mod_chat; touch pwned").mkdir()
    (root / "__pycache__").mkdir()
    shards = integration_shards(["mod_chat"], [], full_scope=True, repo_root=tmp_path)
    assert shards == ["mod_chat", "core"]


def test_conftest_forces_full_scope() -> None:
    scope = build_scope(
        ["tests/conftest.py"],
        graph=GRAPH,
        event="pull_request",
        labels=[],
        full=False,
    )
    assert scope["full"] is True
    assert scope["run_unit"] is True
    assert scope["run_integration"] is True


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


def test_empty_diff_on_pull_request_fails_closed() -> None:
    scope = build_scope(
        [],
        graph=GRAPH,
        event="pull_request",
        labels=[],
        full=False,
    )
    assert scope["mode"] == "code"
    assert scope["full"] is True
    assert scope["run_unit"] is True
    assert scope["run_integration"] is True


def test_write_github_output_serializes_arrays(tmp_path) -> None:
    output = tmp_path / "github-output"
    write_github_output(output, {"modules": ["mod_a", "mod_b"], "run_unit": True})
    text = output.read_text(encoding="utf-8")
    assert 'modules=["mod_a", "mod_b"]' in text
    assert "run_unit=true" in text


def test_sonar_sources_signal_only_for_src_changes() -> None:
    source = build_scope(
        ["src/modules/mod_catalog/application/search.py"],
        graph=GRAPH,
        event="pull_request",
        labels=[],
        full=False,
    )
    assert source["sonar_sources_changed"] is True

    tooling = build_scope(
        ["scripts/ci/scope.py"],
        graph=GRAPH,
        event="pull_request",
        labels=[],
        full=False,
    )
    assert tooling["sonar_sources_changed"] is False


def test_openapi_contract_is_not_docs_only() -> None:
    scope = build_scope(
        ["docs/openapi.json"],
        graph=GRAPH,
        event="pull_request",
        labels=[],
        full=False,
    )
    assert scope["mode"] == "code"
    assert scope["full"] is True
    assert scope["run_validate"] is True
    assert scope["run_unit"] is True


def _git(repo, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def test_diff_files_includes_deleted_and_renamed_paths(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ci@example.com")
    _git(repo, "config", "user.name", "CI")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x(): pass\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / "tests" / "test_x.py").unlink()
    (repo / "README.md").write_text("docs\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "delete test")

    changed = _diff_files(base, repo)
    assert "tests/test_x.py" in changed
    assert "README.md" in changed

    (repo / "src").mkdir()
    (repo / "src" / "code.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add code")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "docs").mkdir()
    _git(repo, "mv", "src/code.py", "docs/code.md")
    _git(repo, "commit", "-m", "rename code to docs")

    changed = _diff_files(base, repo)
    assert "src/code.py" in changed
    assert "docs/code.md" in changed
