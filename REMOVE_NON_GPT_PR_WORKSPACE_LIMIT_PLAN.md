# 拆除非 `gpt/*` PR 分支不能作为可写 workspace 的限制：完整方案

## 1. 目标

当前后端把“可写分支”统一绑定到 `WRITE_BRANCH_PREFIX`，默认是 `gpt/`。这导致已有 PR 的 head 分支如果是 `feature/*`、`fix/*`、`dependabot/*` 等非 `gpt/*` 分支，即使 PR 来自同一个仓库，也不能通过 `prepareWorkspace(source_pr_number=...)` 准备成可写 workspace，后续也不能 `workspaceCommitAndPush`。

本方案的目标是拆掉这个过宽的限制，但不拆掉核心安全边界：

- 仍然只允许同仓库 PR head 分支进入可写 workspace；fork PR 继续拒绝。
- 仍然禁止直接把 `main`、`master`、`develop`、`release/*`、`production/*`、`hotfix/*` 等受保护或高风险分支作为写入目标。
- 仍然要求 `workspaceCommitAndPush.expected_head_sha` 匹配远端 head，避免覆盖别人新推送的提交。
- 仍然保持路径策略、secret 策略、workflow 编辑策略、二进制文件策略不变。
- 仍然让普通新建工作分支走 `gpt/*`，不要把“任意 branch 可写”当成能力开放出去。

一句话：**只放开“同仓、打开状态 PR 的 head 分支”作为可写 workspace，不放开任意非 `gpt/*` 分支写入。**

## 2. 当前限制点

代码阅读结论如下。

### 2.1 分支写策略

`app/policy/rules.py` 中的 `Policy.assert_write_branch_allowed()` 同时做两件事：

1. 禁止 `main`、`master`、`develop`、`release/*`、`production/*`、`hotfix/*`。
2. 要求 branch 必须以 `settings.write_branch_prefix` 开头，默认 `gpt/`。

这导致所有调用者都只能写 `gpt/*`。

### 2.2 workspace prepare

`app/workspace/manager.py` 中 `_prepare_workspace()` 的逻辑是：

- 如果传 `source_pr_number`，先读取 PR。
- 校验 PR head repo 必须是同仓。
- 取 PR head ref 作为 `branch`。
- 只要 `branch is not None`，统一调用 `self.policy.assert_write_branch_allowed(branch)`。

所以同仓 PR head 是 `feature/x` 时，会在这里被拒绝。

### 2.3 commit and push

`app/services/workspaces.py` 的 `WorkspaceService.commit_and_push()` 开头也调用：

```python
self.policy.assert_write_branch_allowed(request.branch)
```

即使 prepare 被放开，提交推送仍会拒绝非 `gpt/*` PR head。

### 2.4 merge PR

`app/services/pulls.py` 的 `PullRequestService.merge_pull_request()` 在 merge 前也调用：

```python
self.policy.assert_write_branch_allowed(info.head_branch)
```

如果目标是“维护一个已有非 `gpt/*` PR，然后显式合并它”，这里也会失败。严格来说这不是 workspace prepare 限制，但属于同一条 `gpt/*` 写边界带来的实际阻塞。

### 2.5 create PR / create work branch

`createWorkBranch` 和 `createPullRequest` 也复用 `assert_write_branch_allowed()`。这两处建议先保持现状：

- `createWorkBranch` 仍只创建 `gpt/*`。
- `createPullRequest` 仍只允许从 `gpt/*` head branch 创建 PR。

原因是本次需求是“处理已有 PR 的非 `gpt/*` head 分支”，不是开放任意分支创建新 PR。

## 3. 推荐目标行为

| 操作 | 分支 | 目标行为 |
| --- | --- | --- |
| `prepareWorkspace(branch="gpt/x")` | `gpt/*` | 允许，保持不变 |
| `prepareWorkspace(branch="feature/x")` | 非 `gpt/*` 普通 branch | 拒绝，保持不变 |
| `prepareWorkspace(base_ref="feature/x")` | 非 `gpt/*` base ref | 只读调查，按 `READ_BRANCH_ALLOWLIST` 或 SHA 策略决定，保持不变 |
| `prepareWorkspace(source_pr_number=N)` | 同仓 PR head = `feature/x` | 允许进入可写 workspace |
| `prepareWorkspace(source_pr_number=N)` | fork PR head | 拒绝，保持不变 |
| `prepareWorkspace(source_pr_number=N)` | head = `main` / `release/*` 等高风险分支 | 拒绝 |
| `workspaceCommitAndPush` | prepared from `gpt/*` branch | 允许，保持不变 |
| `workspaceCommitAndPush` | prepared from same-repo PR head `feature/x` | 允许，但必须重新确认 PR head 仍是这个 branch 且 SHA 未变 |
| `workspaceCommitAndPush` | arbitrary non-PR workspace branch `feature/x` | 拒绝 |
| `createWorkBranch` | non-`gpt/*` | 拒绝，保持不变 |
| `createPullRequest` | non-`gpt/*` head | 建议继续拒绝，保持边界清晰 |
| `mergePullRequest` | same-repo PR head `feature/x` | 建议允许，但保留 open、非 draft、mergeable、expected head SHA 检查 |

## 4. 策略重构方案

### 4.1 不要直接删除 `assert_write_branch_allowed()`

直接把 `assert_write_branch_allowed()` 改成“允许任何非保护分支”风险太大，因为它被多个高风险路径复用：创建分支、创建 PR、commit/push、merge、workspace prepare。

正确做法是把当前“写分支策略”拆成几个意图明确的方法。

### 4.2 新增策略方法

在 `app/policy/rules.py` 中保留现有方法语义，新增更细的策略方法：

```python
def assert_protected_branch_not_writable(self, branch: str) -> None:
    forbidden_exact = {"main", "master", "develop"}
    forbidden_prefixes = ("release/", "production/", "hotfix/")
    if branch in forbidden_exact or branch.startswith(forbidden_prefixes):
        raise ApiError(ErrorCode.BRANCH_NOT_ALLOWED, f"Branch {branch!r} is forbidden for writes.", status_code=403)


def assert_work_branch_allowed(self, branch: str) -> None:
    self.assert_protected_branch_not_writable(branch)
    prefix = self.settings.write_branch_prefix
    if branch == prefix.rstrip("/") or not branch.startswith(prefix):
        raise ApiError(... same current GPT work branch error ...)


def assert_source_pr_head_write_branch_allowed(self, branch: str) -> None:
    self.assert_protected_branch_not_writable(branch)
    if branch == self.settings.write_branch_prefix.rstrip("/"):
        raise ApiError(...)
    # 不要求 gpt/ 前缀；同仓 PR 校验放在 workspace / pull service，因为 policy 不知道 PR repo。
```

然后让现有 `assert_write_branch_allowed()` 暂时作为 `assert_work_branch_allowed()` 的兼容别名，减少一次性改动范围：

```python
def assert_write_branch_allowed(self, branch: str) -> None:
    self.assert_work_branch_allowed(branch)
```

这样可以显式区分：

- “GPT 生成/管理的新工作分支”必须 `gpt/*`。
- “同仓已有 PR 的 head 分支”可以非 `gpt/*`，但必须不是保护分支。

### 4.3 可选配置开关

建议新增配置项，而不是无条件放开：

```python
allow_non_gpt_pr_head_workspaces: bool = True
```

推荐默认值是 `True`，因为本方案目标就是拆掉限制；如果维护者希望保守发布，也可以先默认 `False`，部署环境显式设为 `true`。无论默认值怎么定，都应该写进 `.env.example` 和 README。

如果采用开关，`assert_source_pr_head_write_branch_allowed()` 可接收或读取该配置：

```python
if not self.settings.allow_non_gpt_pr_head_workspaces:
    self.assert_work_branch_allowed(branch)
    return
self.assert_protected_branch_not_writable(branch)
```

## 5. 具体文件修改计划

### 5.1 `app/config/settings.py`

新增配置字段：

```python
allow_non_gpt_pr_head_workspaces: bool = True
```

命名也可以更明确：

```python
allow_same_repo_pr_head_writes: bool = True
```

二选一即可，推荐第二个，因为它强调“same repo PR head”，避免误解成 fork PR 也能写。

最终建议：

```python
allow_same_repo_pr_head_writes: bool = True
```

### 5.2 `.env.example`

新增：

```bash
ALLOW_SAME_REPO_PR_HEAD_WRITES=true
```

并加注释说明：

- 只影响 `prepareWorkspace(source_pr_number=...)` 和基于该 workspace 的 commit/push。
- 不允许 fork PR。
- 不允许 protected/high-risk branch。
- 不改变普通 `branch=` 工作流仍需 `gpt/*` 的限制。

### 5.3 `app/policy/rules.py`

拆分当前 `assert_write_branch_allowed()`：

```python
def assert_protected_branch_not_writable(self, branch: str) -> None:
    ...


def assert_work_branch_allowed(self, branch: str) -> None:
    self.assert_protected_branch_not_writable(branch)
    ... require write_branch_prefix ...


def assert_source_pr_head_write_branch_allowed(self, branch: str) -> None:
    if not self.settings.allow_same_repo_pr_head_writes:
        self.assert_work_branch_allowed(branch)
        return
    self.assert_protected_branch_not_writable(branch)
    if not branch.strip() or branch.endswith("/"):
        raise ApiError(...)


def assert_write_branch_allowed(self, branch: str) -> None:
    self.assert_work_branch_allowed(branch)
```

注意：不要把“同仓 PR”判断塞进 `Policy`，因为 `Policy` 当前只做静态规则，不访问 GitHub；PR repo/state/head SHA 应由 service/manager 处理。

### 5.4 `app/workspace/manager.py`

修改 `_prepare_workspace()` 的 branch 校验分支。

当前伪逻辑：

```python
if source_pr_number is not None:
    source_pr = await self.github.get_pull_request(...)
    ... same repo check ...
    branch = pr.head.ref

if branch is not None:
    self.policy.assert_write_branch_allowed(branch)
    target_ref = branch
else:
    ... read ref ...
```

改为：

```python
prepared_from_source_pr = source_pr_number is not None

if source_pr_number is not None:
    source_pr = await self.github.get_pull_request(...)
    head = source_pr.get("head") or {}
    head_repo = (head.get("repo") or {}).get("full_name", "").lower()
    if head_repo and head_repo != f"{owner}/{repo}".lower():
        raise ApiError(... same current fork rejection ...)
    branch = head.get("ref")
    if not branch:
        raise ApiError(...)
    self.policy.assert_source_pr_head_write_branch_allowed(branch)
elif branch is not None:
    self.policy.assert_work_branch_allowed(branch)
else:
    target_ref = base_ref or default_branch
    self.policy.assert_read_ref_allowed(target_ref)
```

还建议在 `source_pr_number` 路径额外检查：

```python
if source_pr.get("state") != "open":
    raise ApiError(ErrorCode.WORKSPACE_POLICY_VIOLATION, "Only open PR heads can be prepared as writable workspaces.", status_code=403)
```

原因：关闭 PR 的 head 分支可能已过期或被删除；允许写入没有明确协作价值。

如果需要兼容“修复已关闭但未删除的 PR 分支”，这个检查可以不加，但要在风险里说明。

### 5.5 `app/services/workspaces.py`

#### 5.5.1 `commit_and_push()`

把开头的策略校验顺序改成先确认 workspace，再按 workspace 来源判断。

当前：

```python
meta = self._assert_workspace(owner, repo, workspace_id)
self.policy.assert_write_branch_allowed(request.branch)
if request.branch != meta.branch:
    raise ...
```

建议改为：

```python
meta = self._assert_workspace(owner, repo, workspace_id)
if request.branch != meta.branch:
    raise ...

if meta.source_pr_number is not None:
    self.policy.assert_source_pr_head_write_branch_allowed(request.branch)
    await self._assert_source_pr_head_still_matches_workspace(owner, repo, meta.source_pr_number, request.branch)
else:
    self.policy.assert_work_branch_allowed(request.branch)
```

然后新增 helper：

```python
async def _assert_source_pr_head_still_matches_workspace(
    self,
    owner: str,
    repo: str,
    pr_number: int,
    branch: str,
) -> str:
    pr = await self.github.get_pull_request(owner, repo, pr_number)
    if pr.get("state") != "open":
        raise ApiError(ErrorCode.WORKSPACE_POLICY_VIOLATION, "Source PR is no longer open.", status_code=409, details={"pr_number": pr_number})
    head = pr.get("head") or {}
    head_repo = (head.get("repo") or {}).get("full_name", "").lower()
    if head_repo and head_repo != f"{owner}/{repo}".lower():
        raise ApiError(ErrorCode.WORKSPACE_POLICY_VIOLATION, "Source PR head is no longer in the same repository.", status_code=403, details={"pr_number": pr_number})
    if head.get("ref") != branch:
        raise ApiError(ErrorCode.WORKSPACE_POLICY_VIOLATION, "Source PR head branch changed.", status_code=409, details={"pr_number": pr_number, "workspace_branch": branch, "actual_head_branch": head.get("ref")})
    return head.get("sha") or ""
```

之后在已有逻辑中仍保留：

- `fetch_branch(repo_dir, request.branch)`
- `remote_head = remote_head_sha(...)`
- `remote_head == request.expected_head_sha`
- `current_branch == request.branch`
- changed path validation
- push `HEAD:{request.branch}`

建议把 PR head SHA 和 remote head 也对齐检查：

```python
pr_head_sha = await self._assert_source_pr_head_still_matches_workspace(...)
...
remote_head = await self.manager.remote_head_sha(repo_dir, request.branch)
if pr_head_sha and remote_head and pr_head_sha != remote_head:
    raise ApiError(ErrorCode.WORKSPACE_HEAD_CHANGED, "Source PR head SHA does not match remote branch head.", status_code=409, details={...})
```

这样可以避免 GitHub PR metadata 与 branch ref 短暂不一致时误推。

#### 5.5.2 `reset()`

`reset()` 当前只要求 `request.branch == meta.branch`，不额外检查 `gpt/*`。如果 `prepareWorkspace(source_pr_number=...)` 已经准入，那么 reset 可以保持现状。

#### 5.5.3 `apply_patch()` / `write_file()`

这两者不直接检查分支，只检查路径和内容。保持现状即可，因为可写性由 prepare/commit 控制。

如果想更严格，可以新增 `meta.source_pr_number is not None or meta.branch startswith gpt/` 的 workspace 写入断言；但这会改变现有 `base_ref` workspace 的本地临时修改能力，不建议和本次需求混在一起。

### 5.6 `app/services/pulls.py`

#### 5.6.1 `create_pull_request()`

建议保持：

```python
self.policy.assert_work_branch_allowed(request.head_branch)
```

也就是新建 PR 仍必须来自 `gpt/*`。

#### 5.6.2 `merge_pull_request()`

如果希望完整支持“维护并合并已有非 `gpt/*` 同仓 PR”，这里也要放开。

当前：

```python
info = self._info(pr)
self.policy.assert_write_branch_allowed(info.head_branch)
```

改为：

```python
info = self._info(pr)
head = pr.get("head") or {}
head_repo = (head.get("repo") or {}).get("full_name", "").lower()
if head_repo and head_repo == f"{owner}/{repo}".lower():
    self.policy.assert_source_pr_head_write_branch_allowed(info.head_branch)
else:
    self.policy.assert_work_branch_allowed(info.head_branch)
```

是否允许 fork PR merge：建议先不扩大能力。当前 `_info()` 没保存 `head.repo.full_name`，但 `merge_pull_request()` 可以直接用原始 `pr` dict 判断。

需要注意：merge 本身写的是 base branch，不是 head branch；但这套 gateway 当前把 merge 也纳入 `gpt/*` head 边界。若放开同仓非 `gpt/*` PR merge，仍必须保留下面已有检查：

- PR 已打开。
- 非 draft。
- mergeable 不是 false。
- `request.expected_head_sha == info.head_sha`。
- GitHub merge API 使用 `sha=request.expected_head_sha`。

### 5.7 `app/models/workspaces.py`

更新字段说明：

当前：

```python
branch: str | None = Field(default=None, description="gpt/* branch to prepare for read/write maintenance.")
source_pr_number: int | None = Field(default=None, ge=1, description="Prepare from this PR head branch.")
base_ref: str | None = Field(default=None, description="Read-only base branch/ref for investigation.")
```

建议改为：

```python
branch: str | None = Field(default=None, description="gpt/* branch to prepare for read/write maintenance.")
source_pr_number: int | None = Field(default=None, ge=1, description="Prepare a writable workspace from a same-repository PR head branch.")
base_ref: str | None = Field(default=None, description="Read-only base branch/ref for investigation.")
```

这会影响 OpenAPI schema 测试，需同步更新快照/断言。

### 5.8 `README.md` / `PROMPT.md`

文档必须同步，否则调用方 AI 仍会以为所有可写 workspace 都必须 `gpt/*`。

README 中建议把当前安全模型这句：

> work branches and PR heads must use `WRITE_BRANCH_PREFIX`, normally `gpt/`.

改成：

> Generated work branches must use `WRITE_BRANCH_PREFIX`, normally `gpt/`. Existing same-repository PR heads may be prepared as writable workspaces when `ALLOW_SAME_REPO_PR_HEAD_WRITES=true`, but fork PRs and protected/high-risk branches remain blocked.

PROMPT 中建议调整：

- 新任务仍先创建或使用 `gpt/*` 工作分支。
- 继续已有 PR 时可用 `prepareWorkspace(source_pr_number=<pr>)`；如果 PR head 是同仓非 `gpt/*`，后端允许直接提交回该 PR head。
- 不要把非 PR 的普通 `feature/*` 分支作为 `branch=` workspace。

## 6. 测试计划

### 6.1 policy 单元测试

修改/新增 `tests/test_policy.py` 和 `tests/test_policy_v2.py`：

- `assert_work_branch_allowed("gpt/fix")` 允许。
- `assert_work_branch_allowed("feature/fix")` 拒绝。
- `assert_source_pr_head_write_branch_allowed("feature/fix")` 在开关开启时允许。
- `assert_source_pr_head_write_branch_allowed("main")` 拒绝。
- `assert_source_pr_head_write_branch_allowed("release/1.0")` 拒绝。
- 开关关闭时，`assert_source_pr_head_write_branch_allowed("feature/fix")` 拒绝。
- 保留 `assert_write_branch_allowed()` 的老语义，避免旧调用点悄悄扩大权限。

### 6.2 workspace prepare 测试

在 `tests/test_workspace_local_git.py` 中扩展本地仓库 fixture：

- 创建 `feature/pr-head` 分支并推送到 bare remote。
- `LocalGitHub.get_pull_request()` 返回：

```python
{
    "number": 7,
    "state": "open",
    "head": {
        "ref": "feature/pr-head",
        "sha": "...",
        "repo": {"full_name": "acme/demo"},
    },
    "base": {"ref": "main"},
}
```

新增用例：

1. `prepareWorkspace(source_pr_number=7)` 成功，`response.branch == "feature/pr-head"`，`response.source_pr_number == 7`。
2. `prepareWorkspace(branch="feature/pr-head")` 仍失败。
3. `prepareWorkspace(source_pr_number=7)` 对 fork PR 仍失败。
4. PR head 是 `main` / `release/x` 时失败。
5. PR closed 时按最终策略决定：如果加 open 检查，则失败。

### 6.3 commit/push 测试

在 `tests/test_workspace_local_git.py` 新增：

1. 从 `source_pr_number=7` 准备 `feature/pr-head` workspace。
2. 修改 `README.md`。
3. 调用 `workspaceCommitAndPush(branch="feature/pr-head", expected_head_sha=prepared.head_sha, ...)`。
4. 断言 remote `feature/pr-head` 更新到 `response.new_head_sha`。
5. 断言 changed path 策略仍生效，例如 `.env` 写入仍拒绝。

新增 race/防护用例：

- PR head branch 被改成别的 branch，commit/push 应拒绝。
- PR state 改成 closed，commit/push 应拒绝。
- remote branch head 被别人推进，`expected_head_sha` mismatch 应继续返回 `WORKSPACE_HEAD_CHANGED`。
- PR head repo 变成 fork/full_name 不匹配，应拒绝。

### 6.4 merge PR 测试

修改 `tests/test_pulls_merge.py`：

- 当前 `test_merge_pull_request_rejects_non_gpt_head_branch` 需要调整。
- 新增同仓 non-`gpt/*` head 允许 merge：`head_ref="feature/fix-ci"`，`head.repo.full_name="acme/demo"`。
- 新增 fork non-`gpt/*` head 拒绝。
- 保护分支 head 仍拒绝。
- `expected_head_sha` mismatch 仍拒绝。

如果本轮不准备改 merge 行为，就在方案中明确：merge 仍只允许 `gpt/*`，用户维护非 `gpt/*` PR 后需通过 GitHub UI 或后续 PR 改造合并。推荐还是同步改掉，否则“继续已有 PR”流程最后一步会不完整。

### 6.5 OpenAPI / docs 测试

运行：

```powershell
python -m pytest tests/test_policy.py tests/test_policy_v2.py tests/test_workspace_local_git.py tests/test_pulls_merge.py tests/test_openapi_v2.py
```

如耗时可接受，再跑全量：

```powershell
python -m pytest
```

## 7. 审计与响应字段建议

为了让审计更清楚，建议在 audit metadata 中记录 workspace 来源：

- `workspace_branch_kind`: `gpt_work_branch` / `same_repo_pr_head` / `read_only_ref`
- `source_pr_number`
- `source_pr_head_branch`
- `source_pr_head_sha_at_prepare`
- `source_pr_head_sha_at_push`

当前 `WorkspaceMeta` 已有 `source_pr_number`，可以先不改 schema；但 commit/push 审计里建议加 metadata，方便后续追查“为什么这个非 `gpt/*` 分支被允许写”。

## 8. 风险与缓解

### 8.1 写入任意 feature 分支的风险

风险：如果把所有非保护分支都放开，AI 可以直接写 `feature/*`、`bugfix/*`、用户私人分支。

缓解：只在 `source_pr_number` 流程放开，且每次 push 前重新读取 PR，确认 PR 仍 open、同仓、head ref 仍等于 workspace branch。

### 8.2 覆盖别人提交的风险

风险：用户或其他机器人同时向 PR head push。

缓解：继续强制 `expected_head_sha`；push 前 fetch remote；remote head 不等于 expected 时拒绝。

### 8.3 PR metadata 与 git ref 不一致

风险：GitHub PR head SHA 和 refs/heads/branch 短暂不同步。

缓解：commit/push 前同时检查 PR head SHA 与 remote branch head；不一致时拒绝并要求刷新 workspace。

### 8.4 fork PR 安全风险

风险：直接 checkout 并写入 fork PR head 需要跨仓权限，且不应把 token 授权给不受信任的分支。

缓解：保持现有 same-repository check；fork PR 继续拒绝。

### 8.5 受保护分支风险

风险：有人从 `main` 或 `release/*` 开 PR，允许写回会绕过保护流程。

缓解：`assert_source_pr_head_write_branch_allowed()` 仍调用 protected/high-risk branch denylist。

### 8.6 CI 触发差异

当前仓库 workflow 对 push 只监听 `gpt/**`，但 pull_request 监听 base `main`。非 `gpt/*` PR head 被 push 后，GitHub 通常会触发 PR 的 synchronize 事件；不过不要在代码里假设 push workflow 一定存在。创建/更新 PR 后仍用 `queryCiStatus(pr_number=...)` 查询。

## 9. 推荐实施顺序

1. 先加 policy 方法和配置项，不改业务调用点。
2. 改 `WorkspaceManager._prepare_workspace()`，让 `source_pr_number` 使用新 PR head 策略。
3. 改 `WorkspaceService.commit_and_push()`，让 `meta.source_pr_number` workspace 使用新 PR head 策略，并加入 push 前 PR 复核。
4. 改 `PullRequestService.merge_pull_request()`，允许同仓 non-`gpt/*` PR head 合并。
5. 更新模型字段描述、README、PROMPT、`.env.example`。
6. 补测试：policy、prepare、commit/push、merge、OpenAPI。
7. 本地跑定向测试，再跑全量测试。
8. 提交 PR，重点让 reviewer 审查：
   - 是否仍无法通过 `branch="feature/x"` 直接写非 PR 分支。
   - 是否所有 non-`gpt/*` 写入都必须绑定同仓 open PR。
   - 是否每次 push 前重新校验 PR head。
   - 是否 fork PR 和 protected branch 仍拒绝。

## 10. 验收标准

实现完成后，下面行为必须全部满足：

1. `prepareWorkspace(source_pr_number=<同仓 PR，head=feature/x>)` 成功。
2. `workspaceWriteFile` / `workspaceApplyPatch` 可在该 workspace 中修改允许路径。
3. `workspaceCommitAndPush(branch="feature/x", expected_head_sha=<当前 head>)` 成功推送到同一个 PR head branch。
4. `prepareWorkspace(branch="feature/x")` 仍失败。
5. `createWorkBranch(branch="feature/x")` 仍失败。
6. `createPullRequest(head_branch="feature/x")` 仍失败，除非后续明确决定扩大 create PR 能力。
7. fork PR 的 `prepareWorkspace(source_pr_number=...)` 仍失败。
8. PR head 是 `main`、`master`、`develop`、`release/*`、`production/*`、`hotfix/*` 时仍失败。
9. push 前如果 PR head branch、PR state、PR head repo、remote head SHA 任一发生变化，commit/push 拒绝。
10. path policy、secret policy、workflow edit policy、binary policy 的既有测试继续通过。
11. `python -m pytest` 全量通过，或至少上述定向测试全部通过且明确说明未跑全量的原因。

## 11. 最小代码变更摘要

最小可审查 diff 应集中在这些文件：

- `app/config/settings.py`：新增 `allow_same_repo_pr_head_writes`。
- `.env.example`：新增配置示例。
- `app/policy/rules.py`：拆分 GPT work branch 策略与 same-repo PR head 策略。
- `app/workspace/manager.py`：`source_pr_number` 路径使用 PR head 策略，不再套用 `gpt/*` 策略。
- `app/services/workspaces.py`：commit/push 对 `meta.source_pr_number` workspace 使用 PR head 策略，并 push 前复核 PR。
- `app/services/pulls.py`：merge 同仓 PR head 时使用 PR head 策略。
- `app/models/workspaces.py`：更新字段描述。
- `README.md`、`PROMPT.md`：同步能力边界说明。
- `tests/test_policy.py`、`tests/test_policy_v2.py`、`tests/test_workspace_local_git.py`、`tests/test_pulls_merge.py`、`tests/test_openapi_v2.py`：补齐/更新测试。

## 12. 不建议的做法

不要做这些改法：

1. 不要把 `WRITE_BRANCH_PREFIX` 设为空字符串来绕过限制。这会让所有分支看起来都可写，且容易误伤保护分支。
2. 不要直接删除 `assert_write_branch_allowed()` 中的 prefix 检查。它会同时扩大 create branch、create PR、merge、commit/push 等多个入口。
3. 不要允许 fork PR head writable workspace。跨仓写入权限和信任边界完全不同。
4. 不要只改 `prepareWorkspace`。这样虽然能 checkout 非 `gpt/*` PR head，但 `workspaceCommitAndPush` 仍会失败。
5. 不要只依赖 workspace meta 中的 `source_pr_number`。push 前必须重新读取 GitHub PR，防止 PR head 已变更。
