# 考古研究协作基础服务

这是一个供考古项目扩展业务模块的纯后端基础服务，提供研究项目登记、成员与角色、会话认证、审计事件、幂等请求和可恢复后台任务。服务使用 FastAPI 与 SQLite，不依赖另行部署的数据库、缓存或队列。

## 环境与安装

运行环境为 Python 3.11。安装开发依赖：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## 初始化与启动

```bash
python -m app.cli init-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

基础接口包括 `/api/system/health`、`/api/projects`、`/api/users`、`/api/sessions`、`/api/audit` 和 `/api/jobs`。首次启动后可用命令行创建管理员，也可以通过测试夹具构造隔离数据库。

## 测试

```bash
python -m pytest
```

测试覆盖数据库初始化、项目成员权限、会话撤销、审计脱敏、幂等写入和后台任务领取与完成。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内检查根路径、健康接口、数据库外键和 WAL 配置。

## 扩展约定

新研究模块应通过独立路由、服务和仓储接入，跨表写入放在即时事务中。外部标识、幂等键和审计载荷应保存原始值及规范化值；后台任务使用 SQLite 租约，不允许依赖外部队列。用户口令和会话令牌只保存摘要，审计事件会过滤密码、令牌等敏感字段。

## 研究论证与发布模块

模块位于 `app/argumentation/`，围绕“每条结论都能回答：依据是什么、谁审过、后来为什么改变”设计：

- **原子论点与类型化边**：`POST /api/projects/{id}/claims` 创建论点；`POST /api/projects/{id}/edges` 用 `supports`/`rebuts`/`qualifies` 边连接证据版本或其他论点。引用证据时固定到具体版本（`evidence_version`），并保存当时的内容哈希。
- **证据版本化**：`POST /evidence`、`POST /evidence/{ref}/versions`（`replace=true` 时旧版本标记 `superseded`）、`POST /evidence/{ref}/withdraw`。撤回或替换会把引用它的论点标记为 `affected` 并沿论点依赖图传播，历史结论只标记、绝不删除；`GET /claims/affected` 查看受影响论点及原因。
- **环路拒绝**：新增论点依赖边时做环路检测，若成环返回 409 与 `cycle_path`（沿边方向的论点编码路径）。
- **评审流转**：稿件状态机 `draft → in_review → (changes_requested →) approved → published → withdrawn`。作者不能批准自己的稿件；法定人数与合格角色在送审时快照（`PUT /review-strategy` 的后续修改不影响进行中的评审）；同一评审人在同一评审轮次的重复回调幂等返回；并发批准/并发发布通过条件更新与 `publications.manuscript_id` 唯一约束保证只发布一次。
- **发布包**：发布后 `GET /manuscripts/{id}/publication` 返回稳定排序的论点、证据摘要、异议（rebuts/qualifies 边）、评审记录、内容哈希与清单。`python -m app.cli export-publication` 导出、`verify-publication` 离线校验、`verify-audit` 校验审计哈希链、`restart-check` 做重启恢复检查、`evidence-diff` 做版本比较。
- **安全投影**：`visibility=restricted` 的证据仅 owner/researcher 可见完整内容；其他角色在证据、边、受影响原因和发布包中只看到哈希占位与脱敏标记，投影后的发布包会整体重算哈希。

## 验收

```bash
python -m pytest            # 单元与接口测试（含并发发布、安全投影、重启恢复）
bash scripts/acceptance.sh  # 分角色 HTTP + 命令行端到端验收（自动起停服务）
```
