# Gateway Base URL 排查经验

## 适用场景

当 GPT / 外部 Action 调用网关接口失败，且服务端看起来“什么都没响应”时，优先按 `PUBLIC_BASE_URL`、路径前缀映射、请求是否真正进入 uvicorn 这条链路排查。

## 核心判断

如果 uvicorn 没有任何访问日志，优先假设：

- 请求根本没有进入 FastAPI 进程
- 问题在 `PUBLIC_BASE_URL`
- 问题在外层路径前缀映射
- 问题在 ngrok / 反向代理 / Caddy 转发
- 问题在客户端缓存了旧的 OpenAPI server URL

不要先怀疑业务代码。

## 排查顺序

1. 先测健康检查：
   - `curl -i https://<public-host>/<prefix>/healthz`
2. 再测业务路径但不带鉴权：
   - `curl -i -X POST https://<public-host>/<prefix>/repos/<owner>/<repo>/pulls/get`
3. 观察两点：
   - 是否能命中 uvicorn 访问日志
   - 返回的是不是本服务自己的 JSON 错误体

## 判定规则

- `/healthz` 能返回 `200`，说明公网入口和前缀映射基本正常
- 业务路径不带 token 返回 `401 AUTH_FAILED`，说明请求已经进入本服务
- 如果客户端仍声称 `403` / `ClientResponseError`，但服务端没有对应访问日志，优先查外层而不是应用内逻辑

## 本次结论模板

手动验证：

- `GET /github/healthz` 返回 `200`
- `POST /github/repos/.../pulls/get` 不带 token 返回 `401 AUTH_FAILED`

因此可以排除：

- `PUBLIC_BASE_URL` 主机错误
- `/github` 前缀映射错误
- FastAPI 路由未命中

后续应优先排查：

- GPT Action 是否带了正确的 `Authorization: Bearer <configured user token>`
- GPT Action 是否缓存了旧 schema / 旧 server URL
- 外层代理是否对某些请求做了额外拦截
