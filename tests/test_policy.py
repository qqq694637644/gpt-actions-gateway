from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.policy.rules import Policy, normalize_path, sanitize_purpose_slug


def make_policy(**kwargs) -> Policy:
    return Policy(Settings(gpt_action_secret="secret", allowed_repos="acme/demo", **kwargs))


def test_normalize_path_rejects_traversal() -> None:
    with pytest.raises(ApiError):
        normalize_path("../secret.txt")


def test_repo_allowlist() -> None:
    policy = make_policy()
    policy.assert_repo_allowed("acme", "demo")
    with pytest.raises(ApiError) as exc:
        policy.assert_repo_allowed("acme", "other")
    assert exc.value.error_code == ErrorCode.REPO_NOT_ALLOWED


def test_repo_allowlist_can_be_disabled() -> None:
    policy = make_policy(allow_all_repos=True)
    policy.assert_repo_allowed("acme", "other")


def test_write_branch_policy() -> None:
    policy = make_policy()
    policy.assert_write_branch_allowed("gpt/fix-thing")
    with pytest.raises(ApiError):
        policy.assert_write_branch_allowed("main")


def test_workflow_edit_blocked_by_default() -> None:
    policy = make_policy()
    with pytest.raises(ApiError) as exc:
        policy.assert_write_path_allowed(".github/workflows/ci.yml")
    assert exc.value.error_code == ErrorCode.WORKFLOW_EDIT_NOT_ALLOWED


def test_binary_extension_blocked() -> None:
    policy = make_policy()
    with pytest.raises(ApiError) as exc:
        policy.assert_write_path_allowed("assets/logo.png")
    assert exc.value.error_code == ErrorCode.BINARY_FILE_NOT_ALLOWED


def test_tree_path_policy_allows_empty_and_normalizes_prefix() -> None:
    policy = make_policy()
    assert policy.assert_tree_path_allowed(None) is None
    assert policy.assert_tree_path_allowed("src/app") == "src/app"
    with pytest.raises(ApiError):
        policy.assert_tree_path_allowed("../secret")


def test_sanitize_purpose_slug() -> None:
    assert sanitize_purpose_slug("Fix Windows CI!!") == "fix-windows-ci"
