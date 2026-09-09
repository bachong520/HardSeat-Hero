# PostgreSQL 数据库准备清单

## 1. 目标

为云函数 worker 中心任务库准备 PostgreSQL 环境，用于验证 `PostgresCloudCrawlRepository` 的 `claim / lease / retry / dead-letter / response 幂等写入` 行为。

当前准备工作只覆盖中心任务库，不接入 FastAPI 在线搜索，不写入 `data/*.sqlite3`。真实 12306 请求可以在 PostgreSQL 和部署准备完成后，按 sampled probe profile 单独执行。

## 2. 当前项目状态

已有产物：

- `backend/app/cloud_crawl/postgres_schema.sql`：中心任务库 schema。
- `backend/scripts/init_cloud_crawl_postgres.py`：schema 初始化脚本。
- `backend/app/cloud_crawl/postgres_repository.py`：PostgreSQL repository 实现。
- `backend/tests/test_cloud_crawl_postgres_repository.py`：PostgreSQL 集成测试；未设置 `CLOUD_CRAWL_TEST_DATABASE_URL` 时自动跳过。
- `.env.example` 和 `README.md` 已包含 `CLOUD_CRAWL_DATABASE_URL`、`CLOUD_CRAWL_TEST_DATABASE_URL` 示例。

本机当前命令行未检测到：

- `docker`
- `psql`
- `pg_isready`

因此下一步应选择数据库来源并完成初始化，然后执行真实 repository 集成测试。

## 3. 推荐准备顺序

### 3.1 选择 PostgreSQL 来源

二选一即可：

| 方案 | 适用场景 | 要求 |
| --- | --- | --- |
| 本机安装 PostgreSQL | 长期本地开发 | 安装 PostgreSQL 服务端与 `psql` 客户端 |
| 云数据库 / 远程 PostgreSQL | 接近云函数部署环境 | 准备连接串、网络白名单、最小权限账号 |

如果后续安装 Docker，也可以用容器启动本地 PostgreSQL。但当前机器未检测到 `docker`，不把 Docker 作为默认路径。

### 3.2 创建两个库

建议拆分运行库和测试库：

```text
hardseat_cloud_crawl
hardseat_cloud_crawl_test
```

原因：

- `hardseat_cloud_crawl` 用于 sampled_probe 小样本任务。
- `hardseat_cloud_crawl_test` 用于集成测试，测试会清空 `crawl_cloud_*` 表。

### 3.3 配置环境变量

PowerShell 示例：

```powershell
$env:CLOUD_CRAWL_DATABASE_URL = "postgresql://user:password@localhost:5432/hardseat_cloud_crawl"
$env:CLOUD_CRAWL_TEST_DATABASE_URL = "postgresql://user:password@localhost:5432/hardseat_cloud_crawl_test"
```

不要把真实密码提交到 Git。若写入 `.env`，也必须确认 `.env` 已被忽略。

### 3.4 初始化 schema

```powershell
backend\.venv\Scripts\python.exe backend\scripts\init_cloud_crawl_postgres.py
```

或显式传入 DSN：

```powershell
backend\.venv\Scripts\python.exe backend\scripts\init_cloud_crawl_postgres.py `
  --dsn "postgresql://user:password@localhost:5432/hardseat_cloud_crawl"
```

### 3.5 跑 PostgreSQL repository 集成测试

```powershell
backend\.venv\Scripts\python.exe -m pytest backend\tests\test_cloud_crawl_postgres_repository.py
```

通过标准：

- 测试不再因缺少 `CLOUD_CRAWL_TEST_DATABASE_URL` 被 skip。
- `claim` 并发领取不重复。
- 非当前 `lease_token` 不能提交终态。
- retry 达到上限后进入 dead-letter。
- 过期 lease 可回收。
- dry-run release 可用。

### 3.6 只预览 sampled_probe 任务

先不写库：

```powershell
backend\.venv\Scripts\python.exe backend\scripts\prepare_cloud_crawl_sampled_probe.py `
  --input-csv data\seed_od.csv `
  --job-id 1 `
  --provider mock-cloud-price `
  --crawl-date 2026-07-12 `
  --max-tasks 3
```

确认输出后，如需写入中心任务库，才显式增加 `--write`：

```powershell
backend\.venv\Scripts\python.exe backend\scripts\prepare_cloud_crawl_sampled_probe.py `
  --input-csv data\seed_od.csv `
  --job-id 1 `
  --provider mock-cloud-price `
  --crawl-date 2026-07-12 `
  --max-tasks 3 `
  --initialize-schema `
  --write
```

## 4. 当前执行约束

以下事项仍作为工程约束保留，但不再构成项目级门禁：

1. PostgreSQL 集成测试未通过前，不把运行库作为真实 PoC 的中心库。
2. 真实 PoC 初始阶段不提高 worker batch、并发或降低 delay。
3. 不写入 `train_od_fare_edge`。
4. 真实 provider 仅使用 sampled_probe 小样本，发现 HTML/403 或异常错误率时暂停。
5. 不把 `CLOUD_CRAWL_DATABASE_URL` 写成生产库后直接跑测试。

## 5. 下一步执行顺序

按以下顺序执行：

1. 安装本机 PostgreSQL；或
2. 使用已有云数据库 / 远程 PostgreSQL；或
3. 初始化 schema，运行 `test_cloud_crawl_postgres_repository.py`。\n4. 测试通过后准备 sampled_probe 任务。\n5. 完成云函数部署配置，再执行真实 provider PoC。
