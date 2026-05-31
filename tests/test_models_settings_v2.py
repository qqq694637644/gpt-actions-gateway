from app.config.settings import Settings
from app.models.branches import CreateWorkBranchRequest


def test_removed_settings_are_not_present(tmp_path):
    settings = Settings(workspace_root=str(tmp_path / "w"), workspace_mirror_root=str(tmp_path / "m"), audit_db_url=f"sqlite:///{tmp_path / 'audit.db'}")
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
    ]
    for name in removed:
        assert not hasattr(settings, name)


def test_create_work_branch_request_has_no_legacy_aliases():
    schema = CreateWorkBranchRequest.model_json_schema()
    properties = schema["properties"]
    assert "base_branch" not in properties
    assert "source_pr_number" not in properties
