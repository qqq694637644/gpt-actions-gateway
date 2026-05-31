# GPT Actions GitHub Gateway v2

Workspace-first FastAPI gateway for maintaining GitHub repositories through Custom GPT Actions.

v2 uses a backend Git workspace as the only code-maintenance surface:

```text
GPT Action -> Gateway -> backend Git workspace -> controlled PowerShell / Git -> GitHub PR / CI
```

GPT can inspect and edit code by running controlled PowerShell inside a prepared workspace. The gateway owns Git credentials, branch checks, path checks, local change inspection, commit creation, push, PR operations, CI status, logs, artifacts, and audit records.

## Public API surface

### Workspace

- `prepareWorkspace`
- `workspaceExecPwsh`
- `workspaceStatus`
- `workspaceDiff`
- `workspaceCommitAndPush`
- `workspaceReset`

### Branch

- `createWorkBranch`
- `continueWorkBranch`

### Pull Request

- `createPullRequest`
- `getPullRequest`
- `listPullRequests`
- `getPullRequestFiles`
- `updatePullRequest`
- `commentPullRequest`

### CI, logs, and artifacts

- `queryCiStatus`
- `queryFailedCiLog`
- `getCiRun`
- `getCiJobs`
- `getJobLog`
- `getRunLog`
- `listArtifacts`
- `readArtifactText`

## Configuration

Copy `.env.example` to `.env` and set at least:

```bash
APP_ENV=production
PUBLIC_BASE_URL=https://gateway.example.com
GPT_ACTION_SECRET=replace-with-a-long-random-secret
GITHUB_AUTH_MODE=pat
GITHUB_TOKEN=github_pat_xxx
ALLOWED_REPOS=owner/project-a
WORKSPACE_SHELL=pwsh
```

Important workspace settings:

```bash
WORKSPACE_ROOT=./data/workspaces
WORKSPACE_MIRROR_ROOT=./data/mirrors
WORKSPACE_DEFAULT_TIMEOUT_SECONDS=60
WORKSPACE_MAX_TIMEOUT_SECONDS=300
WORKSPACE_MAX_OUTPUT_BYTES=80000
WORKSPACE_MAX_DIFF_BYTES=200000
WORKSPACE_MAX_CHANGED_FILES=200
WORKSPACE_ALLOW_NETWORK=false
WORKSPACE_GIT_USER_NAME=gpt-actions-gateway
WORKSPACE_GIT_USER_EMAIL=gpt-actions-gateway@users.noreply.github.com
```

## Standard workflow

### 1. Create or continue a work branch

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/branches/create-work-branch" \
  -H "Authorization: Bearer $GPT_ACTION_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "base_ref": "main",
    "purpose_slug": "fix-ci"
  }'
```

### 2. Prepare the workspace

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/workspaces/prepare" \
  -H "Authorization: Bearer $GPT_ACTION_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "branch": "gpt/fix-ci-20260531-ab12cd",
    "refresh": true,
    "clean": false,
    "idempotency_key": "task-fix-ci-prepare"
  }'
```

### 3. Inspect, edit, and test inside the workspace

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/workspaces/ws_abc123/exec-pwsh" \
  -H "Authorization: Bearer $GPT_ACTION_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "script": "git status --short; Get-Content pyproject.toml; pytest",
    "timeout_seconds": 120,
    "max_output_bytes": 80000,
    "allow_network": false
  }'
```

`workspaceExecPwsh` always runs from the repository root. It does not receive GitHub credentials and cannot publish code directly.

### 4. Review current state

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/workspaces/ws_abc123/status" \
  -H "Authorization: Bearer $GPT_ACTION_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"refresh": false}'
```

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/workspaces/ws_abc123/diff" \
  -H "Authorization: Bearer $GPT_ACTION_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"paths": ["."], "stat_only": false, "max_bytes": 200000}'
```

### 5. Commit and push

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/workspaces/ws_abc123/commit-and-push" \
  -H "Authorization: Bearer $GPT_ACTION_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "branch": "gpt/fix-ci-20260531-ab12cd",
    "expected_head_sha": "1111111111111111111111111111111111111111",
    "commit_message": "Fix CI setup",
    "paths": ["."],
    "dry_run": false,
    "idempotency_key": "task-fix-ci-commit-1"
  }'
```

The gateway refuses to publish unless the remote branch head equals `expected_head_sha`, the branch is under `gpt/`, changed paths pass policy, and all selected changes are safe to commit.

### 6. Create or reuse a PR

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/pulls/create" \
  -H "Authorization: Bearer $GPT_ACTION_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "head_branch": "gpt/fix-ci-20260531-ab12cd",
    "base_branch": "main",
    "title": "Fix CI setup",
    "body": "Created through GPT Actions Gateway v2."
  }'
```

### 7. Query CI and logs

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/ci/status/query" \
  -H "Authorization: Bearer $GPT_ACTION_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"pr_number": 10}'
```

When CI fails, call `queryFailedCiLog`, then drill into job logs, run logs, or text artifacts. Use `workspaceExecPwsh` again to fix code in the same workspace and publish the next commit through `workspaceCommitAndPush`.

## Security model

- Writes are limited to `gpt/*` branches.
- The gateway refuses sensitive paths such as `.env*`, certificates, credential directories, dependency directories, generated directories, and `.git` internals.
- Workflow files are blocked unless `ALLOW_WORKFLOW_EDIT=true`.
- `workspaceExecPwsh` blocks direct publish commands, GitHub CLI authentication, secret operations, host environment enumeration, SSH/SCP, and network commands unless server policy allows network access.
- GitHub credentials are used only by internal gateway operations.
- Workspace operations are recorded in the audit database.

## Development

```bash
python -m pip install -e '.[dev]'
pytest
python scripts/export_openapi.py
```

`python scripts/export_openapi.py` validates that the generated schema contains exactly the v2 public operation IDs.
