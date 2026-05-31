# GPT Actions GitHub Gateway

这是一个按“任务型 API”实现的后端网关，用于：

```text
Custom GPT Actions → FastAPI Gateway → GitHub REST API → GitHub Actions
```

它不会把 GitHub REST API 原样暴露给 GPT，而是只开放少量安全动作：读文件、列目录、创建 `gpt/*` 工作分支、并发安全提交、创建 PR、按需 merge PR、查询 CI、提取失败日志。默认不允许自动 merge、删除文件、修改 workflow 或重新触发 CI。

## 已实现接口

| 方法 | 路径 | operationId | 说明 |
|---|---|---|---|
| GET | `/repos/{owner}/{repo}/tree` | `listTree` | 列出目录树 / 按路径和扩展名筛选候选文件 |
| POST | `/repos/{owner}/{repo}/snapshots/export` | `exportRepoSnapshot` | 导出仓库快照元数据，可选附带 base64 压缩包 |
| POST | `/repos/{owner}/{repo}/branches/create-work-branch` | `createWorkBranch` | 从 `base_ref` / `base_sha` / `source_pr_number` 创建或继续 `gpt/*` 工作分支 |
| POST | `/repos/{owner}/{repo}/branches/continue-work-branch` | `continueWorkBranch` | 继续一个已存在的 `gpt/*` 工作分支 |
| POST | `/repos/{owner}/{repo}/branches/list` | `listBranches` | 列出仓库分支 |
| POST | `/repos/{owner}/{repo}/branches/get` | `getBranch` | 获取单个分支详情 |
| POST | `/repos/{owner}/{repo}/commits/apply-patch` | `applyPatchAndCommit` | 应用 `git diff / unified diff` 并提交，支持文本 patch、mode change、GIT binary patch |
| POST | `/repos/{owner}/{repo}/compare` | `compareRefs` | 比较两个 ref 的差异 |
| POST | `/repos/{owner}/{repo}/pulls/create` | `createPullRequest` | 创建 PR；若同 head/base 已有 open PR，则返回已有 PR |
| POST | `/repos/{owner}/{repo}/pulls/get` | `getPullRequest` | 获取单个 PR 详情 |
| POST | `/repos/{owner}/{repo}/pulls/list` | `listPullRequests` | 列出 PR |
| POST | `/repos/{owner}/{repo}/pulls/files` | `getPullRequestFiles` | 列出 PR 变更文件 |
| POST | `/repos/{owner}/{repo}/pulls/update` | `updatePullRequest` | 更新 PR 标题、正文、状态或 base 分支 |
| POST | `/repos/{owner}/{repo}/pulls/comment` | `commentPullRequest` | 给 PR 发表评论 |
| POST | `/repos/{owner}/{repo}/pulls/merge` | `mergePullRequest` | 默认关闭；开启后仅允许 merge `gpt/*` 头分支且目标分支在白名单中的 PR |
| POST | `/repos/{owner}/{repo}/metadata/get` | `getRepository` | 获取仓库元数据 |
| POST | `/repos/{owner}/{repo}/metadata/default-branch` | `getDefaultBranch` | 获取默认分支 |
| POST | `/repos/{owner}/{repo}/ci/status/query` | `queryCiStatus` | 通过 JSON body 按 commit / PR / branch 查询并整理 GitHub Actions 状态 |
| POST | `/repos/{owner}/{repo}/ci/failed-log/query` | `queryFailedCiLog` | 通过 JSON body 下载失败 job 日志并提取关键片段 |
| POST | `/repos/{owner}/{repo}/ci/runs/get` | `getCiRun` | 获取单个 workflow run；可选附带 jobs |
| POST | `/repos/{owner}/{repo}/ci/jobs/list` | `getCiJobs` | 列出某个 workflow run 的 jobs |
| POST | `/repos/{owner}/{repo}/ci/jobs/get` | `getCiJob` | 获取单个 workflow job |
| POST | `/repos/{owner}/{repo}/ci/jobs/log` | `getJobLog` | 读取单个 job 日志；可按 step 名称裁剪 |
| POST | `/repos/{owner}/{repo}/ci/runs/log` | `getRunLog` | 读取 workflow run 的日志归档文本文件 |
| POST | `/repos/{owner}/{repo}/ci/workflows/dispatch` | `dispatchWorkflow` | 默认关闭；触发 workflow_dispatch 工作流 |
| POST | `/repos/{owner}/{repo}/ci/runs/rerun` | `rerunWorkflowRun` | 默认关闭；重新运行整个 workflow run |
| POST | `/repos/{owner}/{repo}/ci/runs/rerun-failed` | `rerunFailedJobs` | 默认关闭；仅重新运行失败 jobs |
| POST | `/repos/{owner}/{repo}/ci/jobs/rerun` | `rerunJob` | 默认关闭；重新运行单个 job |
| POST | `/repos/{owner}/{repo}/ci/artifacts/list` | `listArtifacts` | 列出 workflow run 产物 |
| POST | `/repos/{owner}/{repo}/ci/artifacts/read-text` | `readArtifactText` | 从 artifact zip 中读取文本文件 |

## 主要安全设计

- GPT 只拿到网关的 Bearer token，不接触 GitHub token。
- 默认要求仓库在 `ALLOWED_REPOS` 中；设置 `ALLOW_ALL_REPOS=true` 可取消这个限制。
- 写入分支必须以 `WRITE_BRANCH_PREFIX` 开头，默认 `gpt/`。
- 公开 schema 默认使用 `applyPatchAndCommit` 提交修改；它必须带 `expected_head_sha`，提交前检查分支 head，更新 ref 时 `force=false`。
- 隐藏的 `commitFiles` 仍支持 `previous_sha`，可对单文件做更细粒度的并发保护。
- `applyPatchAndCommit` 支持 `modified`、`added`、`deleted`、`renamed`，并支持 blob 文件的 mode change 与 `GIT binary patch`。
- 默认禁止删除文件、修改 `.github/workflows/*`、写入密钥类文件、写入二进制/压缩/构建产物。
- CI 查询优先按 `commit_sha`/PR head SHA 精确匹配；branch 查询会先解析当前 head SHA，避免拿到旧 run。
- CI rerun 默认关闭；开启后还要求 GitHub token 有 Actions: Write。
- 自动 merge 默认关闭；开启后仅允许 merge `gpt/*` 分支发起的 PR。
- SQLite 记录审计事件和幂等响应，避免 GPT Action 重试造成重复分支/提交。

以下后端接口默认仍可用，但不会出现在导出的 GPT Actions OpenAPI 中，避免超过 30 个操作上限并减少模型误选工具：`getFile`、`getFileRange`、`getFiles`、`commitFiles`、`searchCode`、`requestReviewers`、`addLabels`、`getBranchProtection`。

## 快速启动

```bash
cp .env.example .env
# 编辑 .env，至少配置 GPT_ACTION_SECRET、GITHUB_TOKEN、PUBLIC_BASE_URL
# 如需限制仓库范围，配置 ALLOWED_REPOS；如需允许任意仓库，设置 ALLOW_ALL_REPOS=true
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
uvicorn app.main:app --reload
```

健康检查：

```bash
curl http://localhost:8000/healthz
```

导出给 GPT Actions 使用的 OpenAPI：

```bash
python scripts/export_openapi.py
```

## Docker 运行

```bash
cp .env.example .env
# 编辑 .env
 docker compose up --build
```

## GPT Actions 认证配置

在 Custom GPT Actions 中选择 API Key / Bearer token，并填写：

```text
Authorization: Bearer <GPT_ACTION_SECRET>
```

生产环境必须使用 HTTPS、强随机 secret，并定期轮换。

## GitHub token 权限

MVP 使用 fine-grained PAT 时建议：

```text
Contents: Read and write
Pull requests: Read and write
Actions: Read
Metadata: Read
```

开启 `ALLOW_RERUN_CI=true` 时额外需要：

```text
Actions: Write
```

开启 `ALLOW_AUTO_MERGE=true` 时额外需要：

```text
Contents: Write
Pull requests: Write
```

允许修改 `.github/workflows/*` 时额外需要：

```text
Workflows: Write
```

默认不建议开启 workflow 编辑和 CI rerun。

调试接口默认不会挂载到应用中，也不会出现在 OpenAPI。只有在临时排障时，才设置 `ENABLE_DEBUG_ROUTES=true` 并重启服务。

如果部署机器设置了 `HTTP_PROXY` / `HTTPS_PROXY`，网关默认不会继承这些环境代理。只有在确认代理链路可以稳定访问 GitHub API 时，才设置 `GITHUB_USE_ENV_PROXY=true`。

## 推荐目标仓库 CI

将 `github/gpt-validation.workflow.yml` 的内容复制到目标仓库的 `.github/workflows/gpt-validation.yml`，并替换为你的低权限 build/test 命令。关键原则：

- `permissions: contents: read`
- 不在 `gpt/*` 分支 CI 中注入生产 secret
- 不在 `gpt/*` 分支 CI 中 deploy / publish / release
- 不使用 `pull_request_target` 执行 GPT 修改后的代码
- 不使用高权限 self-hosted runner，除非 runner 完全隔离

## 典型调用流程

1. `listTree` 初步判断仓库结构。
2. `exportRepoSnapshot` 导出仓库快照，在本地分析并生成 patch。
3. `createWorkBranch` 创建或继续 `gpt/*` 分支。
4. `applyPatchAndCommit` 提交 patch，传入 `expected_head_sha`。
5. `createPullRequest` 创建 PR。
6. `queryCiStatus` 用 commit SHA / PR 查询 CI。
7. 失败时调用 `queryFailedCiLog`，必要时继续用 `getCiRun` / `getCiJobs` / `getJobLog` / `getRunLog` 下钻。
8. 根据日志修复后再次 `applyPatchAndCommit`。
9. 如需自动 merge，先设置 `ALLOW_AUTO_MERGE=true`，再调用 `mergePullRequest`。

## 示例请求

### 创建工作分支

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/branches/create-work-branch" \
  -H "Authorization: Bearer $GPT_ACTION_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "base_branch": "main",
    "purpose_slug": "fix-windows-ci",
    "initialize_if_empty": true,
    "idempotency_key": "task-20260530-001"
  }'
```

当目标仓库是空仓库时，可以在 `createWorkBranch` 请求中设置 `initialize_if_empty=true`。网关会先在 `base_branch` 上创建一个初始 `README.md` 提交，再继续创建 `gpt/*` 工作分支。

注意：`initialize_if_empty` 是 `createWorkBranch` 的请求体字段，不是 `.env` 配置项。把它写进 `.env` 不会产生任何效果。

### 提交文件

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/commits/commit-files" \
  -H "Authorization: Bearer $GPT_ACTION_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "branch": "gpt/fix-windows-ci-20260530-ab12cd",
    "expected_head_sha": "1111111111111111111111111111111111111111",
    "commit_message": "Fix Windows CI path handling",
    "idempotency_key": "task-20260530-001-commit-1",
    "files": [
      {
        "path": "src/path_utils.py",
        "operation": "upsert",
        "previous_sha": "2222222222222222222222222222222222222222",
        "content": "def normalize(path):\n    return path.replace('\\\\', '/')\n"
      }
    ]
  }'
```

### 应用 patch 并提交

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/commits/apply-patch" \
  -H "Authorization: Bearer $GPT_ACTION_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "branch": "gpt/fix-windows-ci-20260530-ab12cd",
    "expected_head_sha": "1111111111111111111111111111111111111111",
    "commit_message": "Apply generated patch",
    "patch": "diff --git a/src/main.cpp b/src/main.cpp\nindex e69de29..8c84f6f 100644\n--- a/src/main.cpp\n+++ b/src/main.cpp\n@@ -1,0 +1,5 @@\n+#include <iostream>\n+\n+int main() {\n+    std::cout << \\\"Hello\\\" << std::endl;\n+}\n"
  }'
```

当前 `applyPatchAndCommit` 支持：

- `modified`
- `added`
- `deleted`
- `renamed`
- blob 文件的 mode change：`100644`、`100755`、`120000`
- `GIT binary patch`：支持 `literal` 和 `delta`

说明：

- `160000` 这类 submodule 模式不在这个接口支持范围内。
- `dry_run=true` 时只校验并在内存中应用 patch，不会真的创建 commit。

### 合并 PR

```bash
curl -X POST "$PUBLIC_BASE_URL/repos/acme/demo/pulls/merge" \
  -H "Authorization: Bearer $GPT_ACTION_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "pr_number": 4,
    "merge_method": "squash",
    "commit_title": "Merge GPT PR #4",
    "commit_message": "Auto-merged by GPT Actions Gateway after CI passed."
  }'
```

这个接口默认会被拦截。只有在 `.env` 中设置 `ALLOW_AUTO_MERGE=true` 并重启服务后，网关才会放行 merge。

## 结构化错误响应

所有业务错误都会返回：

```json
{
  "error_code": "BRANCH_HEAD_CHANGED",
  "message": "The branch head has changed since the client last read it.",
  "suggestion": "Read the latest branch head / files, then retry commit_files with the new expected_head_sha.",
  "details": {
    "expected_head_sha": "abc",
    "actual_head_sha": "def"
  }
}
```

## 注意事项

- 这个网关是同步 HTTP API，不实现长期后台任务。
- `getFileRange` 默认不暴露在 GPT Actions OpenAPI 中；后端接口仍可用，且读取 blob 时仍受 `MAX_BLOB_READ_BYTES` 限制。
- `CommitFilesResponse.changed_files[].new_sha` 当前不额外查询新 blob SHA，避免多一次 tree 查询；该兼容接口默认不暴露在 GPT Actions OpenAPI 中。
- GitHub App 模式已实现 installation token 获取；生产使用前建议加更完整的安装权限巡检和多 installation 映射。
