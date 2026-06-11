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
- `workspaceApplyPatch`
- `workspaceWriteFile`
- `workspaceCommitAndPush`
- `workspaceReset`

### Branch

- `createWorkBranch`

### Pull Request

- `createPullRequest`
- `getPullRequest`
- `listPullRequests`
- `getPullRequestFiles`
- `updatePullRequest`
- `mergePullRequest`
- `commentPullRequest`

### CI, logs, and artifacts

- `queryCiStatus`
- `dispatchWorkflow`
- `queryFailedCiLog`
- `getCiRun`
- `rerunWorkflowRun`
- `getCiJobs`
- `rerunWorkflowJob`
- `getJobLog`
- `getRunLog`
- `listArtifacts`
- `readArtifactText`
- `listCaches`
- `deleteCache`

## Configuration

Copy `.env.example` to `.env` and set at least:

```bash
APP_ENV=production
PUBLIC_BASE_URL=https://gateway.example.com
GITHUB_AUTH_MODE=pat
GITHUB_TOKEN=replace-with-your-github-token
ALLOWED_REPOS=owner/project-a
WORKSPACE_SHELL=pwsh
AUTH_USERS_JSON='{"users":[{"username":"alice","token_sha256":"<sha256-of-user-token>","roles":["maintainer"],"repos":["owner/project-a"]}]}'
```

### User authorization

Set `AUTH_USERS_JSON` to grant each user a token, role, repository scope, and optional operation overrides. `GPT_ACTION_SECRET` is no longer accepted as a compatibility admin token. Global repository policy still applies, so user access cannot exceed `ALLOWED_REPOS` unless `ALLOW_ALL_REPOS=true`.

```bash
AUTH_USERS_JSON='{
  "users": [
    {
      "username": "alice",
      "token_sha256": "<sha256-of-user-token>",
      "roles": ["reader"],
      "repos": ["owner/project-a"]
    },
    {
      "username": "bob",
      "token_sha256": "<sha256-of-user-token>",
      "roles": ["writer"],
      "repos": ["owner/*"],
      "denied_operations": ["mergePullRequest", "deleteCache"]
    }
  ]
}'
```

Built-in roles:

- `reader`: workspace status/diff, PR reads, CI/log/artifact/cache reads.
- `writer`: `reader` plus workspace prepare/exec/edit/commit, branch creation, PR create/update/comment.
- `ci`: `reader` plus workflow dispatch and reruns.
- `maintainer`: `writer` plus CI write actions, merge, mirror prepare, and cache delete.
- `admin`: all operations. If `repos` is omitted, all repositories are authorized at the user layer.

`operations` and `denied_operations` support exact operation IDs or `fnmatch`-style patterns such as `workspace*`.

Important workspace settings:

```bash
WORKSPACE_ROOT=./data/workspaces
WORKSPACE_MIRROR_ROOT=./data/mirrors
WORKSPACE_DEFAULT_TIMEOUT_SECONDS=60
WORKSPACE_MAX_TIMEOUT_SECONDS=300
WORKSPACE_MAX_OUTPUT_BYTES=80000
WORKSPACE_MAX_DIFF_BYTES=200000
WORKSPACE_MAX_PATCH_BYTES=200000
WORKSPACE_MAX_WRITE_BYTES=200000
WORKSPACE_MAX_CHANGED_FILES=200
WORKSPACE_ALLOW_NETWORK=false
WORKSPACE_GIT_USER_NAME=gpt-actions-gateway
WORKSPACE_GIT_USER_EMAIL=gpt-actions-gateway@users.noreply.github.com
WORKSPACE_PYTHON_VENV_ENABLED=true
WORKSPACE_PYTHON_VENV_DIR=.venv
WORKSPACE_PYTHON_VENV_PYTHON="py -3.13"
WORKSPACE_PYTHON_AUTO_GITIGNORE=true
WORKSPACE_PYTHON_AUTO_ACTIVATE=true
WORKSPACE_PYTHON_AUTO_INSTALL=false
```

When `WORKSPACE_PYTHON_VENV_ENABLED=true`, `prepareWorkspace` automatically prepares a repository-root Python virtual environment for writable `gpt/*` branches and same-repository PR heads. It writes `.venv/` to `.gitignore` when missing, removes accidentally tracked `.venv` files from the Git index with `git rm --cached` while keeping local files, and creates `.venv` with the configured Python command. Existing virtual environments are validated instead of silently accepted. `workspaceExecPwsh` then activates the virtual environment by prepending `.venv/Scripts` or `.venv/bin` to `PATH`; missing, incomplete, or non-executable virtual environments fail before the user script runs. Dependency installation is not implemented yet, so `WORKSPACE_PYTHON_AUTO_INSTALL=true` is rejected until a dependency bootstrap flow is added.

## Standard workflow

### 1. Create a work branch

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/branches/create-work-branch" \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "base_ref": "main",
    "purpose_slug": "fix-ci"
  }'
```

### 2. Prepare the workspace

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/workspaces/prepare" \
  -H "Authorization: Bearer $AUTH_TOKEN" \
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
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "script": "git status --short; Get-Content pyproject.toml; pytest",
    "timeout_seconds": 120,
    "max_output_bytes": 80000,
    "allow_network": false
  }'
```

`workspaceExecPwsh` always runs from the repository root. It does not receive GitHub credentials and cannot publish code directly.

For small auditable edits, use `workspaceApplyPatch` instead of running ad-hoc file mutation scripts:

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/workspaces/ws_abc123/apply-patch" \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "patch": "*** Begin Patch\n*** Update File: README.md\n@@\n-before\n+after\n*** End Patch\n",
    "dry_run": false,
    "allow_delete": false
  }'
```

For a single generated UTF-8 text file, use `workspaceWriteFile`:

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/workspaces/ws_abc123/write-file" \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "path": "docs/ci.md",
    "content": "# CI\n",
    "mode": "create_only",
    "line_ending": "lf",
    "dry_run": false
  }'
```

Both endpoints only modify the local workspace. They never commit, push, create PRs, or trigger CI.

### 4. Review current state

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/workspaces/ws_abc123/status" \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"refresh": false}'
```

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/workspaces/ws_abc123/diff" \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"paths": ["."], "stat_only": false, "max_bytes": 200000}'
```

### 5. Commit and push

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/workspaces/ws_abc123/commit-and-push" \
  -H "Authorization: Bearer $AUTH_TOKEN" \
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
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "head_branch": "gpt/fix-ci-20260531-ab12cd",
    "base_branch": "main",
    "title": "Fix CI setup",
    "body": "Created through GPT Actions Gateway v2."
  }'
```

### 7. Merge the PR

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/pulls/merge" \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "pr_number": 10,
    "merge_method": "squash",
    "commit_title": "Fix CI setup",
    "commit_message": "Merge PR #10"
  }'
```

The gateway only merges open, non-draft PRs whose head branch is still a `gpt/*` work branch.

### 8. Query CI and logs

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/ci/status/query" \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"pr_number": 10}'
```

When CI fails, call `queryFailedCiLog`, then drill into job logs, run logs, or text artifacts. Use `workspaceExecPwsh` again to fix code in the same workspace and publish the next commit through `workspaceCommitAndPush`.

To trigger a `workflow_dispatch` workflow without creating an empty commit, call `dispatchWorkflow` and then query by the returned `query_hint`:

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/ci/workflows/dispatch" \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "workflow_id": "pascal-ci.yml",
    "ref": "gpt/fix-ci-20260531-ab12cd",
    "inputs": {"force_rebuild": "true"},
    "idempotency_key": "task-fix-ci-dispatch-1"
  }'
```

To rerun CI after runner, network, or cache flakiness, use `rerunWorkflowRun` for the whole run or `rerunWorkflowJob` for one failed job:

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/ci/runs/rerun" \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"run_id": 123456789, "enable_debug_logging": false}'
```

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/ci/jobs/rerun" \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"job_id": 987654321, "enable_debug_logging": false}'
```

To inspect and safely clear GitHub Actions caches, list matching caches first, dry run the delete, then delete by `cache_id`:

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/ci/caches/list" \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"key": "ce-lib-", "ref": "refs/heads/gpt/fix-ci-20260531-ab12cd"}'
```

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/ci/caches/delete" \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"cache_id": 12345, "dry_run": true}'
```

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/ci/caches/delete" \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"cache_id": 12345, "dry_run": false, "idempotency_key": "task-delete-cache-12345"}'
```

## Security model

- Writes are limited to `gpt/*` branches.
- The gateway refuses sensitive paths such as `.env*`, certificates, credential directories, dependency directories, generated directories, and `.git` internals.
- Workflow files are blocked unless `ALLOW_WORKFLOW_EDIT=true`.
- `workspaceApplyPatch` and `workspaceWriteFile` reject absolute paths, `..` traversal, `.git` internals, sensitive paths, binary content, oversized payloads, and deletes unless explicitly allowed by request and backend policy.
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
