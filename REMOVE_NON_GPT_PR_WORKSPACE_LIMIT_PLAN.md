# 彻底拆掉 `gpt/*` 写分支限制：个人模式方案与实现说明

## 1. 目标

本仓库原来的写分支策略要求所有可写分支必须以 `WRITE_BRANCH_PREFIX` 开头，默认是 `gpt/`。这会让下面这些个人维护场景被拒绝：

- `prepareWorkspace(branch="feature/x")`
- `workspaceCommitAndPush(branch="feature/x")`
- `createWorkBranch(branch="feature/x")`
- `createPullRequest(head_branch="feature/x")`
- `mergePullRequest` 合并 head branch 为 `feature/x` 的 PR
- 直接维护 `main`、`release/*`、`hotfix/*` 等分支

用户明确要求个人使用，不需要这层分支名前缀边界。因此本 PR 的目标是：**彻底移除后端对写分支名称的 `gpt/*` 限制，不再禁止任意非空分支名。**

## 2. 当前实现决策

### 2.1 已拆掉的限制

`Policy.assert_write_branch_allowed()` 不再检查：

- 分支是否以 `gpt/` 开头。
- 分支是否等于 `main`、`master`、`develop`。
- 分支是否以 `release/`、`production/`、`hotfix/` 开头。

现在它只拒绝空分支名：

```python
if not branch or not branch.strip():
    raise ApiError(ErrorCode.BRANCH_NOT_ALLOWED, "Branch name must be non-empty.", status_code=400)
```

也就是说，`main`、`master`、`develop`、`release/1.0`、`hotfix/x`、`feature/x`、`gpt/x` 都会通过网关的写分支策略。

### 2.2 仍然保留的保护

这次只拆掉“分支名策略”，没有拆掉其他保护：

- `expected_head_sha` 仍然必须匹配远端分支 head，避免静默覆盖别人推送的新提交。
- workspace 仍然必须属于当前 owner/repo。
- `workspaceCommitAndPush` 仍要求 request branch 等于 prepared workspace branch。
- 写路径策略仍然生效，`.env`、secret、credential、依赖目录、生成目录、二进制文件等仍按原策略拒绝。
- workflow 文件编辑仍受 `ALLOW_WORKFLOW_EDIT` 控制。
- 删除文件仍受 `ALLOW_DELETE_FILES` 控制。
- cache 删除仍默认 dry run。
- merge 仍要求 PR open、非 draft、mergeable，且 `expected_head_sha` 等于当前 PR head SHA。

这些不是 `gpt/*` 分支限制，所以保留。

## 3. 代码改动点

### 3.1 `app/policy/rules.py`

把 `assert_write_branch_allowed()` 改为只检查非空分支名。所有复用这个方法的入口都会跟着放开：

- `WorkspaceManager._prepare_workspace()`
- `WorkspaceService.commit_and_push()`
- `BranchService.create_work_branch()`
- `PullRequestService.create_pull_request()`
- `PullRequestService.merge_pull_request()`

### 3.2 `app/models/workspaces.py`

把 `PrepareWorkspaceBaseRequest.branch` 描述从：

```text
gpt/* branch to prepare for read/write maintenance.
```

改为：

```text
Branch to prepare for read/write maintenance.
```

### 3.3 `app/models/branches.py`

把 `CreateWorkBranchRequest.branch` 描述改成：

```text
Optional explicit branch name. If omitted, the gateway generates one using WRITE_BRANCH_PREFIX.
```

因为 `WRITE_BRANCH_PREFIX` 现在只影响自动生成分支名，不再限制显式传入的分支。

### 3.4 `app/api/routes.py`

把 `createWorkBranch` summary 从 `Create or continue a gpt/* work branch` 改成 `Create or continue a work branch`。

### 3.5 `.env.example`

保留：

```bash
WRITE_BRANCH_PREFIX=gpt/
```

但加注释说明它只用于自动生成分支名：

```bash
# Used only for auto-generated branch names. Explicit write branches are not prefix-restricted.
WRITE_BRANCH_PREFIX=gpt/
```

### 3.6 `README.md`

把安全模型、能力分层、发布流程、merge 流程里的 `gpt/*` 写边界说法改成“显式分支不再有前缀限制”。

### 3.7 `PROMPT.md`

把助手约束从“必须先创建或使用 `gpt/*` 工作分支”改为：

```text
先创建或使用任务分支；默认生成 `gpt/*` 分支，但用户明确指定时可使用任意分支。
```

## 4. 行为矩阵

| 操作 | 分支 | 新行为 |
| --- | --- | --- |
| `prepareWorkspace(branch="gpt/x")` | `gpt/*` | 允许 |
| `prepareWorkspace(branch="feature/x")` | 普通分支 | 允许 |
| `prepareWorkspace(branch="main")` | main | 允许 |
| `workspaceCommitAndPush(branch="feature/x")` | 普通分支 | 允许，只要 workspace branch 匹配且 `expected_head_sha` 匹配 |
| `workspaceCommitAndPush(branch="main")` | main | 允许，只要 workspace branch 匹配且 `expected_head_sha` 匹配 |
| `createWorkBranch(branch="feature/x")` | 普通分支 | 允许 |
| `createWorkBranch(branch="main")` | main | 允许；如果分支已存在且 `continue_if_exists=true`，会继续该分支 |
| `createPullRequest(head_branch="feature/x")` | 普通分支 | 允许 |
| `mergePullRequest` head branch = `feature/x` | 普通分支 | 允许，仍要求 PR 状态和 head SHA 校验通过 |
| `assert_write_branch_allowed("")` | 空分支 | 拒绝 |

## 5. 测试覆盖

新增或更新的测试点：

- `tests/test_policy.py`
  - `gpt/fix-thing` 允许。
  - `feature/fix-thing` 允许。
  - `main` 允许。
  - 空分支拒绝。

- `tests/test_policy_v2.py`
  - `main`、`master`、`develop`、`release/1.0`、`production/x`、`hotfix/x`、`feature/x`、`gpt/fix-ci` 都允许。
  - 空分支拒绝。

- `tests/test_branches_enhanced.py`
  - `createWorkBranch(branch="feature/direct-maintenance")` 允许。

- `tests/test_pulls_new.py`
  - `createPullRequest(head_branch="feature/direct-maintenance")` 允许。

- `tests/test_pulls_merge.py`
  - head branch 为 `feature/fix-ci` 的 PR 可以 merge。

- `tests/test_workspace_local_git.py`
  - 本地 bare remote 新增 `feature/task`。
  - `prepareWorkspace(branch="feature/task")` 成功。
  - 修改后 `workspaceCommitAndPush(branch="feature/task")` 成功推送回远端 `feature/task`。

## 6. 风险说明

这是个人模式，风险是明确接受的：

- 网关现在可以按请求写入 `main`、`release/*`、`hotfix/*` 等任意分支。
- `createWorkBranch(branch="main", continue_if_exists=true)` 可能会把已有 `main` 当成可继续的工作分支。
- `workspaceCommitAndPush` 可以直接 push 到任何 prepared branch。
- `createPullRequest` 可以从任意 head branch 创建 PR。
- `mergePullRequest` 不再因为 head branch 不是 `gpt/*` 而拒绝。

仍然建议调用方在执行不可逆操作前保留这些习惯：

- 提交前查 `workspaceDiff`。
- push 前确认 `expected_head_sha` 是刚刚 review 过的 head。
- merge 前重新 `getPullRequest` 和 `queryCiStatus`。

## 7. 不做的事

本 PR 没有把 GitHub Actions workflow 的 push 触发范围从 `gpt/**` 改成所有分支。当前 workflow 仍然只在 `gpt/**` push 和面向 `main` 的 pull_request 上跑。原因是用户要求拆的是后端写分支限制；workflow 触发范围属于 CI 策略，建议单独决定。

如果后续也想让任意分支 push 都触发 CI，可以再改：

```yaml
on:
  push:
    branches:
      - "**"
```

或直接去掉 `push.branches` 过滤。

## 8. 验收标准

实现完成后应满足：

1. `Policy.assert_write_branch_allowed("feature/x")` 不抛错。
2. `Policy.assert_write_branch_allowed("main")` 不抛错。
3. `prepareWorkspace(branch="feature/task")` 成功。
4. `workspaceCommitAndPush(branch="feature/task")` 成功。
5. `createWorkBranch(branch="feature/direct-maintenance")` 成功。
6. `createPullRequest(head_branch="feature/direct-maintenance")` 成功。
7. `mergePullRequest` 不再因为 head branch 非 `gpt/*` 被拒绝。
8. 空分支仍被拒绝。
9. 原有路径、secret、workflow、binary、delete、expected head SHA 等测试继续通过。
