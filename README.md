# 考古研究论证与发布服务

面向发掘项目阶段性认识的研究论证后端：每条结论都能回答“依据是什么、谁审过、后来为什么改变”。服务提供原子化论点、钉版证据引用、失效沿依赖图传播、论证稿评审发布流水线和可离线校验的发布包。纯 FastAPI + SQLite（WAL），不依赖外部数据库、缓存或队列。

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
python -m app.cli create-user --username admin --display-name 项目负责人 --password <至少10位口令>
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

数据库路径与令牌盐由环境变量控制（见 `.env.example`）：`ARCHAEOLOGY_DATABASE_PATH`、`ARCHAEOLOGY_TOKEN_SECRET`。

## 概念与数据模型

- **资源（resources）**：证据载体，分 `sample`（样品）、`feature`（遗迹）、`stat`（统计结果）三类。每次修改产生新的**资源版本**（`resource_versions`），旧版本保留并可比较；资源可整体**撤回**（withdrawn），撤回后禁止新增引用。
- **论点（claims）**：原子化结论，每次修改产生**论点修订**（`claim_revisions`，含修改原因），历史永不删除。
- **论据边（claim_edges）**：`supports` / `rebuts` / `qualifies`，从一个论点指向一个资源（**固定到具体版本**）或另一个论点。边的方向即依赖方向：`from_claim` 依赖目标。
- **受影响标记（claims.affected）**：资源被撤回、或引用版本被新版本替换时，直接引用它的论点被标记为受影响，并沿“被引用方 → 引用方”方向沿依赖图传播；只标记、不自动删除任何历史结论。每次标记/解除都写入 `claim_impacts` 与审计链。重新钉版（`POST /api/edges/{id}/repin`）或撤回边后调用 `POST /api/claims/{id}/resolve` 重算。
- **依赖环**：论点—论点边必须构成无环图。新增边若成环，返回 `409 cycle_detected`，`error.details.path` / `path_codes` 给出造成环路的完整路径。
- **论证稿（arguments）**：一组论点的论证叙述，状态机为 `draft → submitted → (changes_requested → submitted)* → approved → published → retracted`。每次修改产生新修订版（`argument_revisions`）。
- **评审（reviews）**：同一评审人对同一修订版只有一条结论（唯一约束）；重复回调相同结论幂等返回原记录，不同结论返回 409。
- **评审策略快照**：送审时将当前生效策略（法定人数 quorum、可评审角色）固化到稿件上，之后修改策略不影响在审稿件。
- **发布（publications）**：`UNIQUE(argument_id, revision)` + 条件状态迁移保证重复回调与并发审批不会发布两次；重复发布返回同一记录（`replayed: true`）。撤回只改状态，发布包保留可查。

## 角色与权限

| 操作 | owner | researcher | recorder | reviewer | viewer |
|---|---|---|---|---|---|
| 读项目内资源/论点/论证稿 | ✓ | ✓ | ✓ | ✓ | ✓ |
| 登记/版本/撤回资源 | ✓ | ✓ | ✓ | | |
| 提论点、建边、改稿、送审 | ✓ | ✓ | | | |
| 评审（批准/要求修改） | 按策略快照角色 | | | | |
| 设评审策略、发布、撤回发布 | ✓ | | | | |

附加规则：作者不能评审自己的稿件（`403 self_review`）；非项目成员一律 403。

## 安全投影（部分证据不可见）

资源可标记 `visibility: restricted`（如未公布的遗物信息）。`owner` / `researcher` 可见完整内容；其余角色只看到投影：标题显示 `[已屏蔽]`，版本数据与内容哈希置空，但资源标识、钉版版本、失效状态仍可见，论证链保持可追踪。发布包接口同样按调用者角色投影（响应带 `projection: redacted`），完整包的内容哈希不受投影影响。

## 发布包格式与离线校验

发布包（`format: argument-publication/1`）包含：稳定排序（按论点编码）的论点、证据摘要（含钉住版本的内容哈希）、异议（指向包内论点的反驳边 + 要求修改的评审意见）、批准记录、评审策略快照、`content_hash` 与 `manifest`（逐节 sha256 + 整体内容哈希）。

```bash
python -m app.cli export-publication <发布ID> --out package.json   # 导出
python -m app.cli verify-package package.json                      # 离线校验（不访问数据库）
python -m app.cli verify-publication <发布ID>                      # 校验库内发布记录
python -m app.cli verify-audit [--project-id N]                    # 校验审计哈希链
```

审计事件构成哈希链（每条包含前一条的哈希），任何历史篡改都会被 `verify-audit` 或 `GET /api/audit/verify` 发现。审计载荷中的密码、令牌等敏感字段已脱敏。

## 重启恢复

全部状态存于 SQLite（WAL）。启动时 `init_db` 自动执行：建表/迁移（为旧库补审计链列并回填）、回收中断的任务租约（`leased → queued`）。重启后会话、论点、评审状态、发布包与审计链均保持有效，可继续审批与发布。

## API 一览

```
POST /api/users  POST /api/sessions
POST /api/projects  POST /api/projects/{id}/members
GET  /api/audit?project_id=  GET /api/audit/verify
POST /api/projects/{id}/resources            GET /api/projects/{id}/resources
GET  /api/resources/{id}                     POST /api/resources/{id}/versions
POST /api/resources/{id}/withdraw            GET  /api/resources/{id}/diff?from=&to=
POST /api/projects/{id}/claims               GET /api/projects/{id}/claims?affected=
GET  /api/claims/{id}                        PATCH /api/claims/{id}
GET  /api/claims/{id}/revisions              POST /api/claims/{id}/resolve
POST /api/claims/{id}/edges                  POST /api/edges/{id}/repin
POST /api/edges/{id}/withdraw
POST /api/projects/{id}/review-policies      GET /api/projects/{id}/review-policies
POST /api/projects/{id}/arguments            GET /api/projects/{id}/arguments
GET  /api/arguments/{id}                     PUT /api/arguments/{id}
POST /api/arguments/{id}/submit              POST /api/arguments/{id}/reviews
POST /api/arguments/{id}/publish             POST /api/arguments/{id}/retract
GET  /api/arguments/{id}/publication         GET /api/arguments/{id}/diff?from=&to=
POST /api/jobs  POST /api/jobs/claim  POST /api/jobs/{id}/finish
```

创建类接口支持 `Idempotency-Key` 请求头（项目、资源）；发布天然幂等。

## 验收

### 自动化测试

```bash
python -m pytest          # 32 项：权限、钉版、环路、失效传播、评审发布、幂等与并发、安全投影、重启恢复、审计链
python -m compileall -q app tests
python -m app.cli smoke
```

### 分角色 HTTP + 命令行验收

启动服务后执行 `bash scripts/acceptance.sh`（需 `curl` 与 `jq`），依次验收：

1. 五种角色登录，观察员越权写操作返回 403；
2. 记录员登记样品，研究员建论点并钉版引用；
3. 构造论点环返回 409 及环路路径；
4. 资源替换版本后相关论点与下游论点被标记受影响（历史不删除），重新钉版后解除；
5. `GET /api/resources/{id}/diff` 输出字段级版本差异；
6. 论证稿送审（策略快照）、作者自审被 403 拦截、按快照法定人数批准；
7. 重复评审回调与重复发布幂等；发布包含稳定排序论点、证据摘要、异议、内容哈希与清单；
8. `verify-package` 离线校验导出包、`verify-audit` 校验审计链；
9. 撤回后发布包仍可追溯；重启服务后状态与校验结果保持有效（亦可参见 `tests/test_recovery_audit.py`）。

## 目录结构

```
app/
  main.py         路由与错误处理
  service.py      基础服务（用户/会话/项目/任务）+ 审计哈希链基类
  evidence.py     资源版本、论点、论据边、失效传播、安全投影、版本比较
  publication.py  评审策略、论证稿状态机、法定人数快照、幂等发布、发布包与校验
  database.py     SQLite 连接、模式、迁移、租约回收
  security.py     口令/令牌摘要、脱敏、规范化 JSON 与内容哈希
  cli.py          init-db / smoke / verify-* / export-publication / create-user
tests/            pytest 验收套件
scripts/acceptance.sh  分角色 HTTP 验收脚本
```
