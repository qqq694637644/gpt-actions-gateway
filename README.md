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
| POST | `/repos/{owner}/{repo}/files/read` | `getFile` | 读取单个文件，超限截断，二进制不返回正文 |
| POST | `/repos/{owner}/{repo}/files/read-range` | `getFileRange` | 读取指定行范围 |
| POST | `/repos/{owner}/{repo}/files/read-many` | `getFiles` | 一次读取多个文件，限制总大小 |
| POST | `/repos/{owner}/{repo}/branches/create-work-branch` | `createWorkBranch` | 从白名单 base 创建 `gpt/*` 工作分支，支持幂等 |
| POST | `/repos/{owner}/{repo}/commits/commit-files` | `commitFiles` | Git Database API 多文件提交，要求 `expected_head_sha`，`force=false` |
| POST | `/repos/{owner}/{repo}/pulls/create` | `createPullRequest` | 创建 PR；若同 head/base 已有 open PR，则返回已有 PR |
| POST | `/repos/{owner}/{repo}/pulls/merge` | `mergePullRequest` | 默认关闭；开启后仅允许 merge `gpt/*` 头分支且目标分支在白名单中的 PR |
| POST | `/repos/{owner}/{repo}/ci/status/query` | `queryCiStatus` | 通过 JSON body 按 commit / PR / branch 查询并整理 GitHub Actions 状态 |
| POST | `/repos/{owner}/{repo}/ci/failed-log/query` | `queryFailedCiLog` | 通过 JSON body 下载失败 job 日志并提取关键片段 |
| POST | `/repos/{owner}/{repo}/ci/rerun-failed` | `rerunFailedCi` | 默认关闭；开启后仅允许 rerun `gpt/*` 分支的 failed jobs |

## 主要安全设计

- GPT 只拿到网关的 Bearer token，不接触 GitHub token。
- 默认要求仓库在 `ALLOWED_REPOS` 中；设置 `ALLOW_ALL_REPOS=true` 可取消这个限制。
- 写入分支必须以 `WRITE_BRANCH_PREFIX` 开头，默认 `gpt/`。
- `commitFiles` 必须带 `expected_head_sha`；提交前检查分支 head，更新 ref 时 `force=false`。
- 支持 `previous_sha`，可对单文件做更细粒度的并发保护。
- 默认禁止删除文件、修改 `.github/workflows/*`、写入密钥类文件、写入二进制/压缩/构建产物。
- CI 查询优先按 `commit_sha`/PR head SHA 精确匹配；branch 查询会先解析当前 head SHA，避免拿到旧 run。
- CI rerun 默认关闭；开启后还要求 GitHub token 有 Actions: Write。
- 自动 merge 默认关闭；开启后仅允许 merge `gpt/*` 分支发起的 PR。
- SQLite 记录审计事件和幂等响应，避免 GPT Action 重试造成重复分支/提交。

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

1. `listTree` 定位文件。
2. `getFile` / `getFiles` / `getFileRange` 读取上下文。
3. `createWorkBranch` 创建 `gpt/*` 分支。
4. `commitFiles` 提交修改，传入 `expected_head_sha` 和可选 `previous_sha`。
5. `createPullRequest` 创建 PR。
6. `queryCiStatus` 用 commit SHA / PR 查询 CI。
7. 失败时调用 `queryFailedCiLog`。
8. 根据日志再次读取文件并 `commitFiles`。
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
- `getFileRange` 需要先读取 blob，因此仍受 `MAX_BLOB_READ_BYTES` 限制。
- `CommitFilesResponse.changed_files[].new_sha` 当前不额外查询新 blob SHA，避免多一次 tree 查询；commit SHA 和分支 head 已准确返回。
- GitHub App 模式已实现 installation token 获取；生产使用前建议加更完整的安装权限巡检和多 installation 映射。
