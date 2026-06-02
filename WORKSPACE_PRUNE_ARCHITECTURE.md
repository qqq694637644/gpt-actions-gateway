# Workspace TTL Prune Architecture

## Background

The gateway currently protects local workspace storage only with `WORKSPACE_MAX_COUNT`. When the count is reached, new workspace creation fails with `WORKSPACE_STORAGE_LIMIT` even if older workspaces are no longer useful.

`WORKSPACE_TTL_HOURS` already exists in settings and `.env.example`, but it is not used by the implementation. The intended behavior for this single-user workflow is to automatically remove stale backend Git workspaces before creating or preparing another workspace.

## Target workflow

This project is normally used by one operator through a PR-based maintenance flow:

1. prepare a backend workspace;
2. inspect or modify code;
3. commit to a `gpt/*` branch;
4. create a PR;
5. review and merge the PR into the main line.

In this workflow, old local workspaces are disposable after a short period because the durable state is the Git branch, PR, and repository history on GitHub.

## Policy

Use `WORKSPACE_TTL_HOURS=72` as the default retention window.

A workspace is eligible for automatic deletion when all of the following are true:

- its id matches the existing `ws_*` workspace id pattern;
- it has a valid `meta.json`;
- its last workspace use is older than `WORKSPACE_TTL_HOURS`;
- it is not currently marked busy by an existing `lock` file.

The prune operation should delete the whole workspace directory under `WORKSPACE_ROOT`, including its `repo` clone and metadata. It should not delete mirror repositories under `WORKSPACE_MIRROR_ROOT`.

## Last-used timestamp

Prune should be based on last use, not creation time.

Recommended implementation:

- add a `last_used_at` timestamp to workspace metadata;
- update it after successful workspace operations;
- when reading older metadata without `last_used_at`, fall back to the workspace directory modified time or `meta.json` modified time.

Operations that should refresh `last_used_at`:

- `prepareWorkspace`;
- `prepareWorkspaceFromMirror`;
- `workspaceExecPwsh`;
- `workspaceStatus`;
- `workspaceDiff`;
- `workspaceApplyPatch`;
- `workspaceWriteFile`;
- `workspaceCommitAndPush`;
- `workspaceReset`.

## Automatic prune point

Run TTL prune at the start of workspace preparation, before enforcing `WORKSPACE_MAX_COUNT`.

This means `_prepare_workspace()` should do roughly:

```text
prune_expired_workspaces(owner, repo)
enforce_workspace_count()
create or reuse workspace
```

This prevents stale directories from causing `WORKSPACE_STORAGE_LIMIT` when they could be safely removed first.

## Lock handling

Do not introduce a new locking system for prune.

For this single-user deployment, extra deletion locks would add complexity that is not justified by the expected usage pattern. The implementation should only do a lightweight safety check:

- if `<workspace>/lock` exists, skip that workspace;
- do not wait for the lock;
- do not try to break the lock;
- let a future prepare call prune it after it becomes stale and unlocked.

This keeps the deletion path simple while avoiding obvious deletion during an active workspace operation.

## Dry run

A manual prune endpoint may support `dry_run` later, but it is not required for the automatic prepare-time cleanup.

If implemented, `dry_run=true` should only report which workspaces would be deleted and why. It must not remove files. This is useful for manual maintenance, but the minimum fix can be automatic TTL prune only.

## Suggested minimal implementation

1. Change default `workspace_ttl_hours` from `24` to `72` in `Settings` and `.env.example`.
2. Extend `WorkspaceMeta` with optional `last_used_at`.
3. Add helper methods to `WorkspaceManager`:
   - `touch_workspace(workspace_id)`;
   - `workspace_last_used_at(workspace_dir)`;
   - `prune_expired_workspaces(owner, repo)`.
4. Call `prune_expired_workspaces(owner, repo)` before `_enforce_workspace_count()` in `_prepare_workspace()`.
5. Update successful workspace operations to touch the workspace.
6. Add unit tests for:
   - expired workspace is removed before count enforcement;
   - fresh workspace is kept;
   - locked expired workspace is skipped;
   - workspace for a different owner/repo is not deleted by this repo's prepare call;
   - old metadata without `last_used_at` still works through mtime fallback.

## Non-goals

- No new background worker or scheduler.
- No deletion of Git mirrors.
- No deletion of GitHub branches or PRs.
- No new distributed lock or concurrency mechanism.
- No automatic cleanup based on PR merge state.

## Review questions

- Should automatic prune be limited to the same owner/repo, or should it prune all expired workspaces under `WORKSPACE_ROOT`?
- Should corrupt workspaces without valid `meta.json` be ignored, or deleted after TTL based on directory mtime?
- Should the API expose manual `pruneWorkspaces`, or is prepare-time automatic cleanup enough for now?
