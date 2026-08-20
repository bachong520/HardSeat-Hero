# HardSeat Hero

根据出发地、目的地和出行日期，查询低价火车直达与中转方案的 Web 应用原型。

## 技术栈

- 前端：React、TypeScript、Vite、Tailwind CSS、本地 shadcn/ui 风格组件。
- 后端：FastAPI、Pydantic v2、httpx、pytest、SQLite。
- 数据源：通过 `TrainDataProvider` 统一适配 Mock、12306 公布票价接口和 SQLite 静态票价库。
- 默认数据源为 `mock`；可通过 `TRAIN_DATA_PROVIDER=12306-public-price` 试用 12306 公布票价接口，或通过 `TRAIN_DATA_PROVIDER=static-price` 使用本地静态票价库。

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
| `ROUTE_SEGMENT_CACHE_TTL_SECONDS`      | 启动参数默认值                       | 可选，中转段缓存 TTL（秒）。                                                                                     |
| `SEARCH_TELEMETRY_DB`                  | `./data/search_telemetry.sqlite3`    | 可选，搜索遥测 SQLite 路径。                                                                                     |
| `VITE_API_BASE_URL`                    | `http://localhost:8000`              | 前端调用后端 API 的基础地址。                                                                                    |

## 目录

```text
backend/   FastAPI 服务、领域模型、数据源适配器、中转搜索、缓存、遥测和脚本
frontend/  React 查询页面和结果列表
docs/      需求、设计、实施计划、数据源调研和整体架构文档
data/      种子 OD CSV；运行时 SQLite 数据库默认不应提交
```

当前完整架构说明见 `docs/项目整体架构.md`。

## 后端启动

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
uvicorn app.main:app --reload --port 8000
```

默认使用 Mock 数据源。如需试用 12306 公布票价接口：

```powershell
$env:TRAIN_DATA_PROVIDER = "12306-public-price"
uvicorn app.main:app --reload --port 8000
```

该接口不是公开稳定 API，失败时 API 会返回受控错误，Mock 数据源不受影响。

### 可选缓存和遥测

```powershell
$env:ROUTE_SEGMENT_CACHE_DB = ".\data\route_segment_cache.sqlite3"
$env:ROUTE_SEGMENT_CACHE_TTL_SECONDS = "3600"
$env:SEARCH_TELEMETRY_DB = ".\data\search_telemetry.sqlite3"
```

这些 SQLite 文件属于本地运行时数据，已在 `.gitignore` 中忽略。更多环境变量含义见上面的表格。

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

健康检查：`http://localhost:8000/api/health`

方案查询：`POST http://localhost:8000/api/routes/search`，响应包含后端搜索耗时字段 `elapsed_ms`（毫秒）。

## 前端启动

```powershell
cd frontend
pnpm install
pnpm dev
```

默认连接 `http://localhost:8000`。如需修改后端地址，设置 `VITE_API_BASE_URL`，该变量也已写入 `.env.example`。

## 测试

```powershell
cd backend
pytest
```

```powershell
cd frontend
pnpm build
```

本项目前端使用 pnpm 管理依赖。

## 本地联调

分别启动后端和前端：

```powershell
backend\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000 --app-dir backend
```

```powershell
pnpm --dir frontend dev
```

- 前端页面：`http://localhost:5173/`
- 后端 API：`http://127.0.0.1:8000/api`
- 当前 Mock 数据中，北京到上海的最低价中转方案为经 `南京南`，总价 `413.0`。
- 出发地和目的地输入框会调用 `GET /api/stations/search` 提供站点自动补全。
