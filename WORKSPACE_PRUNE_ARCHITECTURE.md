# Workspace TTL 清理架构

## 背景

当前 gateway 对本地 workspace 存储只有 `WORKSPACE_MAX_COUNT` 数量限制。达到上限后，即使旧 workspace 已经没用了，新的 workspace 创建也会因为 `WORKSPACE_STORAGE_LIMIT` 失败。

配置里已经有 `WORKSPACE_TTL_HOURS`，`.env.example` 里也暴露了这个变量，但当前代码没有使用它。

## 使用假设

这个 gateway 主要是单人使用，工作流基于 GitHub PR：

1. 准备一个后端 workspace；
2. 查看或修改代码；
3. 提交到 `gpt/*` 分支；
4. 创建 PR；
5. review 后合并。

真正持久化的状态在 GitHub 上：branch、commit、PR 和主线历史。本地 backend workspace 只是临时缓存。旧 workspace 被删掉后，可以随时从 branch 或 PR 重新 prepare，没有实际损失。

因此清理逻辑应该尽量简单：基于文件夹最后修改时间删除，不需要按项目归属、metadata、PR 状态或额外的 last-used 字段做复杂判断。

## 清理策略

默认使用：

```text
WORKSPACE_TTL_HOURS=48
```

workspace 满足以下条件时可以自动删除：

- 它是 `WORKSPACE_ROOT` 直属目录；
- 目录名匹配现有 `ws_*` workspace id 规则；
- 目录最后修改时间超过 `WORKSPACE_TTL_HOURS`；
- 目录下当前没有 `lock` 文件。

删除时直接删除整个 workspace 目录，包括里面的 `repo` clone 和 metadata。

不要删除 `WORKSPACE_MIRROR_ROOT` 下的 mirror 仓库。

## 自动清理时机

在 `prepareWorkspace` 创建或复用 workspace 前自动清理过期 workspace，并且要发生在 `_enforce_workspace_count()` 之前。

`_prepare_workspace()` 逻辑大致是：

```text
prune_expired_workspace_dirs()
enforce_workspace_count()
create or reuse workspace
```

这样可以避免旧 workspace 堆积导致 `WORKSPACE_MAX_COUNT` 先报错，而不是先清理。

## 使用目录修改时间

只使用 workspace 目录自身的 filesystem modified time 作为最后使用时间。

不新增 `last_used_at` 字段。

不在每个 workspace 操作后额外更新 metadata。

在当前单人 PR 工作流里，长时间没有目录变动的 workspace 可以认为是旧缓存，删除后可以从 GitHub 重新拉取。

## lock 处理

不新增删除锁，也不引入新的并发控制机制。

唯一安全检查是：

- 如果 `<workspace>/lock` 存在，就跳过该 workspace。

不要等待 lock，不要打破 lock，也不要和正在执行的操作做额外协调。后续再调用 `prepareWorkspace` 时，如果 lock 已不存在且目录仍然过期，再删除即可。

## 建议的最小实现

1. 把 `Settings` 和 `.env.example` 里的默认 `workspace_ttl_hours` 从 `24` 改成 `48`。
2. 在 `WorkspaceManager` 里新增一个小方法，例如 `prune_expired_workspace_dirs()`。
3. 该方法只扫描：

   ```python
   self.root.glob("ws_*")
   ```

4. 对每个候选目录：
   - 跳过非目录；
   - 跳过不匹配 `WORKSPACE_ID_RE` 的名字；
   - 如果 `<dir>/lock` 存在则跳过；
   - 用 `dir.stat().st_mtime` 和 `now - workspace_ttl_hours` 比较；
   - 过期则 `shutil.rmtree(dir)`。
5. 在 prepare 新 workspace 前调用该方法，再执行 `_enforce_workspace_count()`。
6. 增加测试：
   - 旧 `ws_*` 目录会在数量限制检查前被删除；
   - 新 `ws_*` 目录会保留；
   - 有 lock 的旧 `ws_*` 目录会跳过；
   - 非 `ws_*` 目录会忽略；
   - mirror 目录不会被删除。

## 非目标

- 不做后台 worker 或 scheduler。
- 第一版不需要手动 prune API。
- 自动清理不需要 `dry_run`。
- 不按 owner/repo 过滤。
- 不做 metadata 迁移。
- 不新增 `last_used_at`。
- 不清理 GitHub branch、PR 或根据 merge 状态清理。
- 不清理 mirror。
