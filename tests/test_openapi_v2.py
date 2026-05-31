from app.main import app
from scripts.export_openapi import PUBLIC_OPERATION_IDS, collect_operation_ids


def test_openapi_contains_only_v2_operation_ids():
    schema = app.openapi()
    assert collect_operation_ids(schema) == PUBLIC_OPERATION_IDS
    assert len(PUBLIC_OPERATION_IDS) == 30


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
    }
    schema = app.openapi()
    assert collect_operation_ids(schema).isdisjoint(hidden_or_removed)
