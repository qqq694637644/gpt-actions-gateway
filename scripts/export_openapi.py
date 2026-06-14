from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OPENAPI_SERVER_URL = "https://estranged-evergreen-hatchet.ngrok-free.dev/github"

PUBLIC_OPERATION_IDS = {
    "prepareWorkspace",
    "workspaceExecPwsh",
    "workspaceStatus",
    "workspaceDiff",
    "workspaceApplyPatch",
    "workspaceWriteFile",
    "workspaceCommitAndPush",
    "workspaceReset",
    "createWorkBranch",
    "createPullRequest",
    "getPullRequest",
    "listPullRequests",
    "getPullRequestFiles",
    "updatePullRequest",
    "mergePullRequest",
    "commentPullRequest",
    "queryCiStatus",
    "dispatchWorkflow",
    "queryFailedCiLog",
    "getCiRun",
    "rerunWorkflowRun",
    "getCiJobs",
    "rerunWorkflowJob",
    "getJobLog",
    "getRunLog",
    "listArtifacts",
    "syncRunArtifactsToWorkspace",
    "listCaches",
    "deleteCache",
}

READ_ONLY_OPERATION_IDS = {
    "workspaceStatus",
    "workspaceDiff",
    "getPullRequest",
    "listPullRequests",
    "getPullRequestFiles",
    "queryCiStatus",
    "queryFailedCiLog",
    "getCiRun",
    "getCiJobs",
    "getJobLog",
    "getRunLog",
    "listArtifacts",
    "listCaches",
}

CONSEQUENTIAL_OPERATION_IDS = PUBLIC_OPERATION_IDS - READ_ONLY_OPERATION_IDS


def collect_operation_ids(schema: dict) -> set[str]:
    operation_ids: set[str] = set()
    for path_item in schema.get("paths", {}).values():
        for method, operation in path_item.items():
            if method.lower() in {"get", "post", "put", "patch", "delete"} and "operationId" in operation:
                operation_ids.add(operation["operationId"])
    return operation_ids


def validate_public_operations(schema: dict) -> None:
    operation_ids = collect_operation_ids(schema)
    extra = operation_ids - PUBLIC_OPERATION_IDS
    missing = PUBLIC_OPERATION_IDS - operation_ids
    if extra or missing:
        raise SystemExit(f"OpenAPI v2 operationId validation failed. extra={sorted(extra)} missing={sorted(missing)}")


def mark_operations_by_risk(schema: dict) -> None:
    classified = READ_ONLY_OPERATION_IDS | CONSEQUENTIAL_OPERATION_IDS
    if classified != PUBLIC_OPERATION_IDS:
        raise SystemExit(
            "OpenAPI risk classification is incomplete. "
            f"missing={sorted(PUBLIC_OPERATION_IDS - classified)} extra={sorted(classified - PUBLIC_OPERATION_IDS)}"
        )
    for path_item in schema.get("paths", {}).values():
        for method, operation in path_item.items():
            if method.lower() in {"get", "post", "put", "patch", "delete"} and isinstance(operation, dict) and "operationId" in operation:
                operation_id = operation["operationId"]
                if operation_id not in classified:
                    raise SystemExit(f"OpenAPI operationId is not risk-classified: {operation_id}")
                operation["x-openai-isConsequential"] = operation_id in CONSEQUENTIAL_OPERATION_IDS


def main() -> None:
    from app.main import app

    schema = app.openapi()
    schema["servers"] = [{"url": OPENAPI_SERVER_URL}]
    validate_public_operations(schema)
    mark_operations_by_risk(schema)
    out = ROOT / "openapi.json"
    out.write_text(json.dumps(schema, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {out.resolve()}")


if __name__ == "__main__":
    main()
