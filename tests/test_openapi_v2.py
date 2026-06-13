from app.main import app
from scripts.export_openapi import PUBLIC_OPERATION_IDS, collect_operation_ids, mark_all_operations_nonconsequential


def test_openapi_contains_only_v2_operation_ids():
    schema = app.openapi()
    assert collect_operation_ids(schema) == PUBLIC_OPERATION_IDS
    assert len(PUBLIC_OPERATION_IDS) == 29


def test_hidden_and_removed_operation_ids_are_absent():
    hidden_or_removed = {
        "prepareWorkspaceMirror",
        "prepareWorkspaceFromMirror",
        "continueWorkBranch",
        "listTree",
        "exportRepoSnapshot",
        "applyPatchAndCommit",
        "commitFiles",
        "getFile",
        "getFileRange",
        "getFiles",
        "searchCode",
        "compareRefs",
        "listBranches",
        "getBranch",
        "getBranchProtection",
        "getRepository",
        "getDefaultBranch",
        "rerunFailedJobs",
        "rerunJob",
        "getCiJob",
    }
    schema = app.openapi()
    assert collect_operation_ids(schema).isdisjoint(hidden_or_removed)


def test_export_marks_all_public_operations_nonconsequential():
    schema = app.openapi()

    mark_all_operations_nonconsequential(schema)

    for path_item in schema["paths"].values():
        for method, operation in path_item.items():
            if method.lower() in {"get", "post", "put", "patch", "delete"} and "operationId" in operation:
                assert operation["x-openai-isConsequential"] is False


def test_workspace_exec_pwsh_response_excludes_workspace_change_summary():
    schema = app.openapi()
    operation = schema["paths"]["/repos/{owner}/{repo}/workspaces/{workspace_id}/exec-pwsh"]["post"]
    response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    schema_name = response_schema["$ref"].rsplit("/", 1)[-1]
    properties = schema["components"]["schemas"][schema_name]["properties"]

    assert set(properties) == {"exit_code", "stdout", "stderr", "truncated", "duration_ms"}
    assert "changed_files" not in properties
    assert "diff_stat" not in properties
