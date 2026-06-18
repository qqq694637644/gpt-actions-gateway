# GPT Actions Gitea Gateway v2

A task-oriented FastAPI gateway for GPT Actions that operates against the Gitea REST API and Gitea Actions. The gateway keeps the workspace-first safety model from the original GitHub implementation: code is read, edited, tested, committed, and pushed through backend Git workspaces instead of ad-hoc remote mutations.

## What changed for Gitea

This branch replaces the GitHub API client with a Gitea client while preserving the public GPT Action operation IDs. Existing OpenAPI tools such as `prepareWorkspace`, `createWorkBranch`, `createPullRequest`, `queryCiStatus`, and `workspaceCommitAndPush` remain stable.

Key API differences handled by the gateway:

- Branch creation uses `POST /repos/{owner}/{repo}/branches` with `new_branch_name` and `old_ref_name`.
- Git refs use Gitea's plural `GET /repos/{owner}/{repo}/git/refs/{ref}` endpoint.
- Pull request merge uses `POST /repos/{owner}/{repo}/pulls/{index}/merge` with Gitea's `do`, `head_commit_id`, `merge_title_field`, and `merge_message_field` payload fields.
- Workflow dispatch requests `return_run_details=true` when Gitea supports returning run details.
- Gitea does not expose a GitHub-compatible workflow run log archive endpoint, so `getRunLog` builds a zip archive from per-job logs.
- Gitea artifact metadata may not include a remote digest. The gateway still validates zip paths and records a computed `sha256:` digest after download.
- The inspected Gitea API spec does not expose GitHub-compatible Actions cache list/delete endpoints. `listCaches` and `deleteCache` are kept for schema compatibility but return a clear unsupported error with the Gitea client.

The old `app.github.*` modules are compatibility shims that import the new `app.gitea.*` implementation. Prefer `GITEA_*` settings for new deployments.

## Configuration

Copy `.env.example` to `.env` and set at least:

```env
PUBLIC_BASE_URL=https://gateway.example.com
GPT_ACTION_SECRET=replace-with-a-long-random-secret

GITEA_API_BASE_URL=https://gitea.example.com/api/v1
GITEA_TOKEN=replace-with-your-gitea-personal-access-token
GITEA_GIT_USERNAME=gitea-username

ALLOW_ALL_REPOS=false
ALLOWED_REPOS=owner/project-a
WRITE_BRANCH_PREFIX=gpt/
DEFAULT_BASE_BRANCH=main
```

`GITEA_API_BASE_URL` must include the `/api/v1` suffix. The gateway derives HTTPS Git remotes and commit URLs by stripping that suffix.

Deprecated `GITHUB_TOKEN` and `GITHUB_GIT_USERNAME` are accepted only as migration fallbacks when the new Gitea variables are not set. GitHub App authentication is not supported for Gitea.

## Safety model

The gateway enforces repository and path policy before touching a workspace or remote branch.

- Configure `ALLOWED_REPOS` unless `ALLOW_ALL_REPOS=true` is explicitly reviewed.
- Writes are expected on task branches, normally under the `gpt/` prefix.
- Workflow edits under `.gitea/workflows/*` and `.github/workflows/*` require `ALLOW_WORKFLOW_EDIT=true`.
- Secret files, dependency directories, generated directories, local virtualenvs, and binary-like files are blocked by policy.
- `workspaceExecPwsh` runs inside the repository, blocks credential/environment enumeration, and only allows network access when both the request and server configuration allow it.
- `.gpt-artifacts/` is added to `.git/info/exclude` when artifacts are synced into a workspace, so synced artifacts remain local and are not committed.

## Public operations

Workspace:

- `prepareWorkspace`
- `workspaceExecPwsh`
- `workspaceStatus`
- `workspaceDiff`
- `workspaceApplyPatch`
- `workspaceWriteFile`
- `workspaceCommitAndPush`
- `workspaceReset`

Branches and pull requests:

- `createWorkBranch`
- `createPullRequest`
- `getPullRequest`
- `listPullRequests`
- `getPullRequestFiles`
- `updatePullRequest`
- `mergePullRequest`
- `commentPullRequest`

Gitea Actions and artifacts:

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
- `syncRunArtifactsToWorkspace`
- `listCaches` (schema-compatible; unsupported by the inspected Gitea API)
- `deleteCache` (schema-compatible; unsupported by the inspected Gitea API)

## Development

Install development dependencies and run tests:

```powershell
python -m pip install -e .[dev]
python -m pytest
ruff check .
```

Export the GPT Actions OpenAPI schema:

```powershell
python scripts/export_openapi.py
```

The exporter validates that the public operation IDs remain unchanged.
