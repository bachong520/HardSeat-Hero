# HardSeat Hero

根据出发地、目的地和出行日期，查询低价火车直达与中转方案的 Web 应用原型。

## 技术栈

### 前端
- **React** 19.2.7
- **TypeScript** 6.0.3
- **Vite** 8.1.0
- **Tailwind CSS** 4.3.1
- **包管理器**: pnpm 11.3.0
- **UI**: 本地 shadcn/ui 风格组件（使用 lucide-react、clsx、tailwind-merge）

### 后端
- **FastAPI** >=0.115.0
- **Python** >=3.11
- **Pydantic** v2 (>=2.8.0)
- **httpx** >=0.27.0
- **pytest** >=8.2.0
- **psycopg** >=3.2.0（PostgreSQL 驱动，用于云函数中心任务库）
- **数据库**: SQLite（本地静态票价库、缓存、遥测）/ PostgreSQL（云函数中心任务库）

### 数据源
- 通过 `TrainDataProvider` 统一适配：
  - **Mock 数据源**（默认，用于本地开发）
  - **12306 公布票价接口**（`12306-public-price`，非官方稳定 API）
  - **SQLite 静态票价库**（`static-price`，本地缓存历史票价）
  - **全量快照模式**（`full-snapshot-*`，用于 time-expanded 图搜索）

## 环境变量

仓库根目录提供了 `.env.example`，可以复制为 `.env` 后按需修改。当前项目实际使用到的环境变量如下：

| 变量                                   | 默认值                               | 说明                                                                                                             |
| -------------------------------------- | ------------------------------------ | ---------------------------------------------------------------------------------------------------------------- |
| `TRAIN_DATA_PROVIDER`                  | `mock`                               | 后端数据源。可选 `mock`、`12306-public-price`、`static-price`。                                                  |
| `STATIC_PRICE_DB`                      | `./data/static_prices.sqlite3`       | 当 `TRAIN_DATA_PROVIDER=static-price` 时必填，本地静态票价库路径。                                               |
| `STATIC_PRICE_MODE`                    | `static-only`                        | 静态票价库模式。可选 `static-only`、`local-only`、`static-with-remote-fallback`、`remote-fallback`、`fallback`。 |
| `STATIC_PRICE_FALLBACK_PROVIDER`       | `12306-public-price`                 | `STATIC_PRICE_MODE` 需要远程回填时使用的数据源。                                                                 |
| `STATIC_PRICE_MAX_AGE_DAYS`            | 空                                   | 静态票价最大可接受年龄（天）；空值表示不限制。                                                                   |
| `ROUTE_SEARCH_ENGINE`                  | `online`                             | 路线搜索引擎。正式值为 `online`、`time-expanded`；`candidate` 等历史值仅作为在线引擎兼容别名。                    |
| `TIME_EXPANDED_GRAPH_DB`               | `./data/static_prices.sqlite3`       | 当 `ROUTE_SEARCH_ENGINE=time-expanded` 时必填；优先于 `STATIC_PRICE_DB`。                                        |
| `TIME_EXPANDED_GRAPH_PROVIDER`         | `12306-public-price`                 | `time-expanded` 模式下读取的来源标识。                                                                           |
| `ROUTE_SEGMENT_CACHE_DB`               | `./data/route_segment_cache.sqlite3` | 可选，本地中转段缓存 SQLite 路径。                                                                               |
| `ROUTE_SEGMENT_CACHE_TTL_SECONDS`      | `3600`                               | 可选，中转段缓存 TTL（秒）。                                                                                     |
| `SEARCH_TELEMETRY_DB`                  | `./data/search_telemetry.sqlite3`    | 可选，搜索遥测 SQLite 路径。                                                                                     |
| `CLOUD_CRAWL_DATABASE_URL`             | 空                                   | 可选，云函数 worker 中心任务库 PostgreSQL DSN；为空时 handler 默认使用内存 repository。                          |
| `CLOUD_CRAWL_TEST_DATABASE_URL`        | 空                                   | 可选，PostgreSQL repository 集成测试 DSN；为空时相关测试自动跳过。                                               |
| `CLOUD_CRAWL_DEFAULT_BATCH_SIZE`       | `5`                                  | 可选，云函数 worker 默认 claim batch 大小。                                                                      |
| `CLOUD_CRAWL_MAX_TASKS_PER_INVOCATION` | `10`                                 | 可选，单次 worker invocation 最大处理任务数。                                                                    |
| `CLOUD_CRAWL_DEFAULT_CONCURRENCY`      | `1`                                  | 可选，单实例 worker 并发数。                                                                                     |
| `CLOUD_CRAWL_DEFAULT_DELAY_MS`         | `1000`                               | 可选，worker 请求间隔毫秒。                                                                                      |
| `CLOUD_CRAWL_TIMEOUT_SECONDS`          | `60`                                 | 可选，worker 单请求超时秒数。                                                                                    |
| `CLOUD_CRAWL_LEASE_SECONDS`            | `300`                                | 可选，worker 任务 lease 秒数。                                                                                   |
| `CLOUD_CRAWL_DRY_RUN`                  | `true`                               | 可选，云函数 worker dry-run 开关。                                                                               |
| `VITE_API_BASE_URL`                    | `http://localhost:8000`              | 前端调用后端 API 的基础地址。                                                                                    |

## 目录

```text
backend/         FastAPI 服务、领域模型、数据源适配器、中转搜索引擎、云函数 worker、缓存、遥测和脚本工具
  ├── app/       核心应用代码
  │   ├── adapters/      数据源适配器（Mock、12306、静态票价库）
  │   ├── api/           FastAPI 路由和端点
  │   ├── cloud_crawl/   云函数 worker 和 PostgreSQL repository
  │   ├── models/        Pydantic 数据模型
  │   └── search/        路线搜索引擎（online、time-expanded）
  ├── scripts/   数据爬取、刷新、巡检等脚本工具
  └── tests/     单元测试和集成测试
frontend/        React 查询页面和结果列表
  ├── src/       源代码（组件、API 客户端、类型定义）
  └── dist/      构建产物（不提交）
docs/            需求、设计、实施计划、数据源调研和整体架构文档
  ├── analysis/  需求分析和调研
  ├── architecture/ 架构设计
  ├── designs/   详细设计
  ├── plans/     实施计划
  └── research/  技术调研
data/            种子 OD CSV；运行时 SQLite 数据库默认不提交（在 .gitignore 中）
  ├── seed_od.csv           种子 OD 清单（提交到 Git）
  └── *.sqlite3             运行时数据库（不提交，本地生成）
```

当前完整架构说明见 `docs/项目整体架构.md`。

## 快速开始

### 后端启动

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
uvicorn app.main:app --reload --port 8000
```

**注意**：12306 公布票价接口不是公开稳定 API，失败时 API 会返回受控错误，Mock 数据源不受影响。

### 前端启动

```powershell
cd frontend
pnpm install
pnpm dev
```

默认连接 `http://localhost:8000`。如需修改后端地址，设置 `VITE_API_BASE_URL`。

### 本地联调

分别启动后端和前端：

```powershell
# 终端 1：后端
backend\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000 --app-dir backend
```

```powershell
# 终端 2：前端
pnpm --dir frontend dev
```

- **前端页面**: `http://localhost:5173/`
- **后端 API**: `http://127.0.0.1:8000/api`
- **健康检查**: `http://localhost:8000/api/health`
- **方案查询**: `POST http://localhost:8000/api/routes/search`，响应包含后端搜索耗时字段 `elapsed_ms`（毫秒）

当前 Mock 数据中，北京到上海的最低价中转方案为经 `南京南`，总价 `413.0`。

**注意**：出发地和目的地输入框保留手动输入；站点自动补全已从当前 API 范围移除，后续作为独立能力重新设计。

## 高级配置

### 数据源切换

默认使用 Mock 数据源。如需试用 12306 公布票价接口：

```powershell
$env:TRAIN_DATA_PROVIDER = "12306-public-price"
uvicorn app.main:app --reload --port 8000
```

### 缓存和遥测

启用中转段缓存和搜索遥测：

```powershell
$env:ROUTE_SEGMENT_CACHE_DB = ".\data\route_segment_cache.sqlite3"
$env:ROUTE_SEGMENT_CACHE_TTL_SECONDS = "3600"
$env:SEARCH_TELEMETRY_DB = ".\data\search_telemetry.sqlite3"
```

这些 SQLite 文件属于本地运行时数据，已在 `.gitignore` 中忽略。

### 云函数 worker PostgreSQL 中心任务库

初始化中心任务库 schema：

```powershell
$env:CLOUD_CRAWL_DATABASE_URL = "postgresql://user:password@localhost:5432/hardseat_cloud_crawl"
backend\.venv\Scripts\python.exe backend\scripts\init_cloud_crawl_postgres.py
```

运行 PostgreSQL repository 集成测试：

```powershell
$env:CLOUD_CRAWL_TEST_DATABASE_URL = "postgresql://user:password@localhost:5432/hardseat_cloud_crawl_test"
backend\.venv\Scripts\python.exe -m pytest backend\tests\test_cloud_crawl_postgres_repository.py
```

未设置 `CLOUD_CRAWL_DATABASE_URL` 时，云函数 handler 仍使用内存 repository 和 mock provider，避免误连真实中心库。

准备 sampled_probe 小样本任务，默认只预览、不写库、不访问 12306：

```powershell
backend\.venv\Scripts\python.exe backend\scripts\prepare_cloud_crawl_sampled_probe.py `
  --input-csv data\seed_od.csv `
  --job-id 1 `
  --provider mock-cloud-price `
  --crawl-date 2026-07-12 `
  --max-tasks 3
```

确认样本后，如需写入 PostgreSQL 中心任务库，必须显式增加 `--write`，并提前设置 `CLOUD_CRAWL_DATABASE_URL`。该脚本只写中心任务库，不写 `data/*.sqlite3`。

### 搜索引擎切换

后端路线搜索通过 `ROUTE_SEARCH_ENGINE` 选择搜索架构。未设置时默认使用 `online` 在线 OD 搜索。

在线 OD 搜索（默认）：

```powershell
$env:ROUTE_SEARCH_ENGINE = "online"
uvicorn app.main:app --reload --port 8000
```

`online` 使用统一优先队列覆盖直达、一换和二换，`max_transfers` 只限制路径扩展。边数据通过 `TrainDataProvider.search_segments()` 懒加载，并受候选站策略、缓存和远程请求预算限制，因此不等同于全国完整铁路图搜索，也不承诺完整图意义上的全局最优。`candidate`、`od-candidate`、`legacy` 等名称只为兼容既有环境变量。

本地时间扩展图搜索：

```powershell
$env:ROUTE_SEARCH_ENGINE = "time-expanded"
$env:TIME_EXPANDED_GRAPH_DB = ".\data\static_prices.sqlite3"
$env:TIME_EXPANDED_GRAPH_PROVIDER = "12306-public-price"
uvicorn app.main:app --reload --port 8000
```

`time-expanded` 查询只读本地 SQLite 的时间扩展图快照表，不触发 12306 远程 fallback，也不调用 `TrainDataProvider.search_segments()`。本地数据缺失时返回无方案；覆盖不足需要通过静态库诊断和后续离线刷新流程处理。静态公布票价不等于实时有票，当前时间扩展图也不宣称具备全国完整覆盖。

全量快照爬取基础边：

```powershell
backend\.venv\Scripts\python.exe backend\scripts\crawl_time_expanded_full_snapshot.py `
  --db data\static_prices.sqlite3 `
  --provider full-snapshot `
  --date 2026-07-12 `
  --train-info-concurrency 4 `
  --price-concurrency 2 `
  --train-info-delay 0.3 `
  --price-delay 0.8 `
  --max-retries 5 `
  --resume
```

小规模验证：

```powershell
backend\.venv\Scripts\python.exe backend\scripts\crawl_time_expanded_full_snapshot.py `
  --db data\static_prices.sqlite3 `
  --provider full-snapshot-dev `
  --date 2026-07-12 `
  --limit-trains 20 `
  --price-concurrency 1 `
  --price-delay 1.0 `
  --resume
```

只统计任务规模，不请求票价、不写基础边：

```powershell
backend\.venv\Scripts\python.exe backend\scripts\crawl_time_expanded_full_snapshot.py `
  --db data\static_prices.sqlite3 `
  --provider full-snapshot-dry-run `
  --date 2026-07-12 `
  --dry-run
```

主要控制参数：`--limit-trains`、`--max-stops-per-train`、`--max-od-per-train`、`--min-station-gap`、`--include-seat-types`、`--train-info-concurrency`、`--price-concurrency`、`--train-info-delay`、`--price-delay`、`--max-retries`、`--resume`、`--retry-failed`。

全量快照脚本会写入 `train_od_fare_edge` 基础边。`time-expanded` 搜索优先读取该表，并把相对时刻按查询日期展开为 3 天窗口。快速版本假设已导入车次默认每天可用；不处理运营日历、临时停运、实时余票或可购票状态。

### 静态票价库模式

如果已经通过 `backend/scripts/refresh_static_prices.py` 刷新过本地静态票价库，可以使用 `static-price` provider 查询 SQLite 本地数据。

静态票价库文件（例如 `data/static_prices.sqlite3`）属于本地运行时数据，不应提交到 Git。仓库只跟踪 `data/seed_od.csv` 这类种子输入；首次使用静态库模式前，需要按下方“手动刷新静态库示例”在本机生成或更新 SQLite 文件。

推荐使用相对路径或统一的仓库内路径，避免从项目根目录和 `backend/` 目录启动时解析到不同数据库文件。

只读本地静态库：

```powershell
$env:STATIC_PRICE_DB = ".\data\static_prices.sqlite3"
$env:STATIC_PRICE_MODE = "static-only"
$env:STATIC_PRICE_MAX_AGE_DAYS = "30"
uvicorn app.main:app --reload --port 8000
```

本地未命中或过期时远程补洞并写回静态库：

```powershell
$env:STATIC_PRICE_DB = ".\data\static_prices.sqlite3"
$env:STATIC_PRICE_MODE = "static-with-remote-fallback"
$env:STATIC_PRICE_FALLBACK_PROVIDER = "12306-public-price"
$env:STATIC_PRICE_MAX_AGE_DAYS = "30"
uvicorn app.main:app --reload --port 8000
```

注意：`static-with-remote-fallback` 会在查询缺失或过期 OD 时访问 12306 公布票价接口，可能变慢、失败或被限流。该模式只保证公布票价查询，不保证实时有票。

手动刷新静态库示例：

```powershell
cd backend
python scripts/refresh_static_prices.py --date 2026-07-01 --od-file ../data/seed_od.csv --db ../data/static_prices.sqlite3 --interval-seconds 1
```

查看静态库整体覆盖：

```powershell
cd backend
python scripts/inspect_static_prices.py --db ../data/static_prices.sqlite3
```

查看指定 OD 静态票价明细：

```powershell
cd backend
python scripts/inspect_static_prices.py --db ../data/static_prices.sqlite3 --date 2026-07-01 --from 北京 --to 上海
```

如果已启用 `SEARCH_TELEMETRY_DB`，可从搜索遥测导出下一批待刷新 OD：

```powershell
cd backend
python scripts/export_refresh_od_from_telemetry.py --telemetry-db ../data/search_telemetry.sqlite3 --output ../data/refresh_od.csv
```

## 测试

### 后端测试

```powershell
cd backend
pytest
```

### 前端构建验证

```powershell
cd frontend
pnpm build
```

## 开发工具

项目提供了多个脚本工具，位于 `backend/scripts/` 目录：

- `crawl_time_expanded_full_snapshot.py` - 爬取全量快照用于 time-expanded 图搜索
- `refresh_static_prices.py` - 刷新本地静态票价库
- `inspect_static_prices.py` - 巡检静态票价库覆盖和明细
- `export_refresh_od_from_telemetry.py` - 从遥测数据导出待刷新 OD
- `init_cloud_crawl_postgres.py` - 初始化云函数中心任务库 schema
- `prepare_cloud_crawl_sampled_probe.py` - 准备小样本云函数任务
- `prepare_cloud_poc_job.py` - 准备完整 POC job
- `check_cloud_poc_gate.py` - 检查 POC gate 和任务覆盖
- `import_station_metadata.py` - 导入车站元数据
- `probe_12306_pricing.py` - 探测 12306 定价接口
- `compare_adjacent_vs_direct_price.py` - 比较相邻段与直达段票价

## 许可证

本项目为个人学习和原型验证项目，不得用于商业用途。

## 注意事项

1. **数据源限制**：12306 公布票价接口不是官方稳定 API，可能随时失效或变更，仅供学习和验证使用。
2. **本地数据**：`data/*.sqlite3` 文件属于运行时生成的本地数据，已在 `.gitignore` 中忽略，不应提交到版本控制。
3. **Python 版本**：要求 Python >=3.11。
4. **包管理器**：前端使用 pnpm，后端使用 pip + pyproject.toml。
5. **CORS 配置**：当前 CORS 仅允许 `http://localhost:5173` 和 `http://127.0.0.1:5173`，生产环境需调整。
