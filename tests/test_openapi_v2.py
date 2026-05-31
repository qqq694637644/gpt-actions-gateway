from app.main import app
from scripts.export_openapi import PUBLIC_OPERATION_IDS, collect_operation_ids


def test_openapi_contains_only_v2_operation_ids():
    schema = app.openapi()
    assert collect_operation_ids(schema) == PUBLIC_OPERATION_IDS


def test_removed_operation_ids_are_absent():
    removed = {
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
        "dispatchWorkflow",
        "rerunWorkflowRun",
        "rerunFailedJobs",
        "rerunJob",
        "getCiJob",
    }
    schema = app.openapi()
    assert collect_operation_ids(schema).isdisjoint(removed)
