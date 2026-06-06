import pytest
from pydantic import ValidationError

from app.config.settings import Settings
from app.models.branches import CreateWorkBranchRequest
from app.models.workspaces import PrepareWorkspaceFromMirrorRequest, PrepareWorkspaceRequest
from app.workspace.ids import WORKSPACE_ID_PATTERN


def make_settings(tmp_path, **kwargs) -> Settings:
    return Settings(workspace_root=str(tmp_path / "w"), workspace_mirror_root=str(tmp_path / "m"), audit_db_url=f"sqlite:///{tmp_path / 'audit.db'}", **kwargs)


def test_removed_settings_are_not_present(tmp_path):
    settings = make_settings(tmp_path)
    removed = [
        "max_snapshot_bytes",
        "max_search_file_bytes",
        "max_file_size",
        "max_total_read_size",
        "max_total_commit_size",
        "max_files_per_commit",
        "allow_rerun_ci",
        "allow_auto_merge",
        "enable_debug_routes",
        "base_branch_allowlist",
        "base_branch_patterns",
    ]
    for name in removed:
        assert not hasattr(settings, name)


def test_create_work_branch_request_has_no_legacy_aliases():
    schema = CreateWorkBranchRequest.model_json_schema()
    properties = schema["properties"]
    assert "base_branch" not in properties
    assert "source_pr_number" not in properties


def test_prepare_workspace_request_workspace_id_schema_requires_ws_prefix():
    schema = PrepareWorkspaceRequest.model_json_schema()
    workspace_id = schema["properties"]["workspace_id"]
    string_branch = next(item for item in workspace_id["anyOf"] if item.get("type") == "string")
    assert string_branch["pattern"] == WORKSPACE_ID_PATTERN

    mirror_schema = PrepareWorkspaceFromMirrorRequest.model_json_schema()
    mirror_workspace_id = mirror_schema["properties"]["workspace_id"]
    mirror_string_branch = next(item for item in mirror_workspace_id["anyOf"] if item.get("type") == "string")
    assert mirror_string_branch["pattern"] == WORKSPACE_ID_PATTERN


@pytest.mark.parametrize(
    "value",
    [
        r"C:\temp\venv",
        "C:/temp/venv",
        "C:temp/venv",
        "/tmp/venv",
        r"\\server\share\venv",
        "tools//venv",
        "tools:venv",
        "../venv",
    ],
)
def test_workspace_python_venv_dir_rejects_absolute_drive_and_invalid_paths(tmp_path, value: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(tmp_path, workspace_python_venv_dir=value)


def test_workspace_python_venv_dir_normalizes_relative_trailing_slash(tmp_path) -> None:
    settings = make_settings(tmp_path, workspace_python_venv_dir="tools/.venv/")

    assert settings.workspace_python_venv_dir == "tools/.venv"


def test_workspace_python_auto_install_true_fails_until_implemented(tmp_path) -> None:
    with pytest.raises(ValidationError, match="WORKSPACE_PYTHON_AUTO_INSTALL is not implemented"):
        make_settings(tmp_path, workspace_python_auto_install=True)
