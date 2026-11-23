# 云函数 worker 真实接口 PoC 报告（2026-11-23）

## 0. 结论速览

| 问题 | 回答 |
| --- | --- |
| 控制流跑通了吗 | 跑通了：PG 认领 → 真实请求 → 错误分类 → 回写状态，3/3 成功，2.3 秒完成 |
| 采集结果留下什么 | job 1：**只有行数和哈希，一行原始数据都没留**；job 2（修复后重跑）：3 条响应摘要各约 7.4~7.7 KB 已入库，见第 14 节 |
| 可以进入阶段 5 并发测绘吗 | **不可以**。先补"结果落库"，重跑一次再谈升档（摘要落库已完成并验证，但阶段 6 合并链路仍未跑通） |
| 本次判定 | 通过（控制流），但结果留存不满足模板第 10 节预期 |

一句话：**管道通了，但管道末端是开着的。**

## 1. 使用边界

模板第 1 节列了 6 条"不应执行"，本次逐条对照如下：

| 模板约束 | 本次 | 说明 |
| --- | --- | --- |
| 不访问 `12306` | **已放开** | 阶段 4 的目的就是访问真实接口，且已按小样本、单并发执行 |
| 不运行云函数 worker 真实 provider | **已放开** | 同上，运行方式为本地受控命令，未部署到云 |
| 不运行本地爬虫脚本 | 遵守 | 未运行 `refresh_static_prices.py` 等既有爬虫 |
| 不写入 `data/*.sqlite3` | 遵守 | 所有状态只写 PostgreSQL，未触碰本地 SQLite |
| 不生成大批量 station OD 任务 | 遵守 | 仅 3 条任务 |
| 不写入 `train_od_fare_edge` | 遵守 | `edge_count` 三条全为 0 |

前两条属于"获批后放开"的边界，其余四条本次全程守住。**这也是本次报告能成立的前提**：如果这两条没守住，报告就只是又一次未授权的抓取记录。

## 2. PoC 基本信息

| 项目 | 内容 |
| --- | --- |
| PoC 编号 | `cloud-worker-poc-20261123-001` |
| 执行日期 | 2026-11-23 |
| 执行人 | bachong520（本地受控执行） |
| 目标环境 | 本地（Windows + venv），未部署云函数 |
| 云厂商与区域 | 无（本次不涉及） |
| job_id | 1 |
| provider | `12306-public-price` |
| crawl_date | 2026-11-27 |
| task_mode | `sampled_probe` |
| 中心库 DSN 来源 | `CLOUD_CRAWL_DATABASE_URL`（本地 PostgreSQL 19.0，`hardseat_cloud`） |
| repository 实现 | `PostgresCloudCrawlRepository` |
| 样本来源 | `data/seed_od.csv`（3 条 OD） |
| 样本任务数 | 3 |
| 是否写基础边 | 否（`edge_count` 全 0） |
| 是否写 SQLite | 否 |

## 3. 执行前检查清单

| 检查项 | 必须满足 | 结果 | 备注 |
| --- | --- | --- | --- |
| 本地采集状态 | 本地长跑采集已结束，或确认不会与云端请求冲突 | 通过 | 本地未运行任何采集任务 |
| 外部请求压力 | 当前没有其他 12306 高频采集任务运行 | 通过 | 仅本 worker 单进程 |
| 中心任务库 | PostgreSQL/MySQL/云数据库任务表可用 | 通过 | PG 19.0，4 张表已建 |
| claim/lease/retry/dead-letter | 真实 repository 已验证 | 通过 | 3 条任务 `attempts=1`，无重领 |
| PostgreSQL 集成测试 | 对应测试库已通过 | **跳过** | 未配置 `CLOUD_CRAWL_TEST_DATABASE_URL`，全量测试里该模块 7 项 skip |
| 暂停机制 | 可立即停止调度器或 worker | 通过 | 前台进程，Ctrl+C 即停；lease 300 秒后自动回收 |
| 样本任务 | 只使用 sampled_probe 小样本 | 通过 | 3 条 |
| profile 固化 | 不临时提高并发、不降低 delay | 通过 | 见第 4 节 |
| 停止条件 | HTML/403、成功率、p95、dead-letter 条件已确认 | 通过 | 见第 6 节 |
| 结果隔离 | 只写响应摘要或对象存储，不写基础边 | **部分不满足** | 未写基础边（符合），但响应摘要也没写（见第 10 节） |
| 日志记录 | 已准备记录本模板要求的指标 | 通过 | 见第 8 节 |

执行结论：

```text
允许执行（已执行完成），但"结果隔离"一项事后判定为部分不满足，已在第 10、11 节记录
```

门禁脚本 `backend/scripts/check_cloud_poc_gate.py` 本次输出 `gate_result=PASS`（14 项全 PASS）。

> 注：门禁脚本的 `DEFAULT_REPORT_TEMPLATE` 是相对**当前工作目录**的路径，在 `backend/` 下运行会误报 `FAIL poc_report_template`。本次通过显式传参绕过：
> `--report-template "..\docs\plans\云函数worker真实接口PoC报告模板.md"`。
> 这属于脚本缺陷（默认路径应按 `BACKEND_ROOT.parent` 解析），已记入第 12 节待办，**实验中途没有改动它**。

## 4. 固定运行 profile

初始 profile：

```text
task_mode=sampled_probe
batch_size=5
max_tasks_per_invocation=10
single_instance_concurrency=1
request_delay_ms=1000
timeout_seconds=60
lease_seconds=300
```

本次实际 profile（**比初始更保守，未升档**）：

| 参数 | 值 | 是否符合初始 profile | 备注 |
| --- | ---: | --- | --- |
| `batch_size` | 3 | 符合（低于上限） | 命令显式指定 |
| `max_tasks_per_invocation` | 3 | 符合（低于上限） | 与样本数一致 |
| `single_instance_concurrency` | 1 | 符合 | |
| `request_delay_ms` | 1000 | 符合 | 实际间隔见第 8 节 |
| `timeout_seconds` | 30 | 符合（更严格） | 比初始 60 秒更保守 |
| `lease_seconds` | 300 | 符合 | 覆盖租约的逻辑有回归测试（见 `backend/tests/test_cloud_crawl_repository.py`） |
| 云函数实例数 | 1 | 符合 | 本地单进程 |

## 5. 样本任务说明

| 字段 | 内容 |
| --- | --- |
| 样本生成方式 | 手写 CSV → `backend/scripts/prepare_cloud_poc_job.py` 导入 |
| 样本 OD 数 | 3 |
| 车站范围 | 北京 / 上海 / 南京南（东部干线热门站，非随机抽样） |
| 是否包含热门 OD | 是（北京→上海） |
| 是否包含边界 OD | 否 |
| 是否去重 | 是（脚本按 OD 去重） |
| 是否可复现 | 是（CSV 与脚本参数均记录在 7.1） |
| 生成脚本 | `backend/scripts/prepare_cloud_poc_job.py` |
| 是否先预览 | 是（先不带 `--write` 预览，再带 `--write` 落库） |

样本清单摘要：

```text
from_station,to_station,note
北京,上海,单日 52 趟车
北京,南京南,单日 73 趟车
南京南,上海,单日 335 趟车
```

> 站码未落库：任务表 `from_station_code` / `to_station_code` 两列为空，请求时由 worker 从 `station_name.js` 现解析。这是本次观察到的一个小缺口（可复现性变差：三个月后若 12306 改了站码，无法复原本次请求参数）。

## 6. 停止条件

| 条件 | 阈值 | 实际值 | 是否触发 | 处理动作 |
| --- | ---: | ---: | --- | --- |
| 成功率 | `< 98%` | 100%（3/3） | 否 | — |
| p95 latency | `> 90s` | 0.807s | 否 | — |
| 非 JSON / HTML 比例 | `> 1%` | 0% | 否 | — |
| HTTP 403 | 连续出现即停止 | 0 次 | 否 | — |
| dead-letter 比例 | `> 0.5%` | 0% | 否 | — |
| 云函数超时 | 任一批次超时需暂停分析 | 0 次 | 否 | — |
| 中心库异常 | claim/commit 异常需暂停分析 | 0 次 | 否 | — |

停止结论：

```text
未触发停止
```

## 7. 执行记录

### 7.1 启动命令或触发方式

```powershell
# 0) 中心库
$env:CLOUD_CRAWL_DATABASE_URL = "postgresql://hardseat:hardseat_dev_pw@localhost:5432/hardseat_cloud"
python scripts\init_cloud_crawl_postgres.py

# 1) 造 job（先预览，再 --write）
python scripts\prepare_cloud_poc_job.py --input-csv ..\data\seed_od.csv `
  --crawl-date 2026-11-27 --max-tasks 3
python scripts\prepare_cloud_poc_job.py --input-csv ..\data\seed_od.csv `
  --crawl-date 2026-11-27 --max-tasks 3 --write

# 2) 过门禁
python scripts\check_cloud_poc_gate.py --job-id 1 --provider 12306-public-price `
  --crawl-date 2026-11-27 --task-mode sampled_probe `
  --report-template "..\docs\plans\云函数worker真实接口PoC报告模板.md"

# 3) 跑 worker（真实 provider）
python scripts\run_cloud_price_worker_local.py --job-id 1 `
  --provider 12306-public-price --crawl-date 2026-11-27 `
  --worker-id local-worker-1 --provider-kind railway-12306 --allow-real-provider `
  --repository postgres --batch-size 3 --max-tasks-per-invocation 3 `
  --single-instance-concurrency 1 --request-delay-ms 1000 `
  --timeout-seconds 30 --lease-seconds 300
```

不含任何密钥（DSN 中的口令为本地开发库口令，非生产凭据）。

### 7.2 时间线

| 时间（北京时间） | 事件 | 备注 |
| --- | --- | --- |
| 20:10:21.793 | 创建 job（`crawl_cloud_job` id=1） | `task_mode=sampled_probe` |
| 20:12:33.652 | 开始执行，首批 claim 3 条任务 | `claimed_count=3` |
| 20:12:33~35 | 3 条任务依次请求成功 | 每请求间隔 ≥1000ms |
| 20:12:35.965 | 全部完成，进程退出 | `succeeded_count=3` |
| 20:12:33 ~ | 首个 retry_wait | 无 |
| 20:12:33 ~ | 首个 dead_letter | 无 |

## 8. 指标汇总

| 指标 | 值 |
| --- | ---: |
| `sample_count` | 3 |
| `claimed_count` | 3 |
| `processed_count` | 3 |
| `succeeded_count` | 3 |
| `skipped_count` | 0 |
| `retry_wait_count` | 0 |
| `dead_letter_count` | 0 |
| `timeout_count` | 0 |
| `success_rate` | 100% |
| `p50_latency_ms` | 514 |
| `p95_latency_ms` | 807 |
| `p99_latency_ms` | 不适用（样本 3，无统计意义） |
| `effective_success_rps` | 1.30（3 成功 / 2.313 秒总耗时） |
| `html_response_count` | 0 |
| `http_403_count` | 0 |
| `non_json_count` | 0 |
| `provider_error_count` | 0 |
| `db_claim_error_count` | 0 |
| `db_commit_error_count` | 0 |

行数交叉验证：探测脚本（`urllib` + 同步实现）与 cloud worker（`httpx` + async + 数据库租约）对"北京→上海 2026-11-27"给出同一个数字 **52 趟**。两套独立实现结果一致，说明请求参数、站码解析、JSON 解析三处都没有偏差。

## 9. 错误分类

| 错误类型 | 数量 | 示例 task_id | 示例 OD | 是否可重试 | 处理结论 |
| --- | ---: | --- | --- | --- | --- |
| 网络超时 | 0 | — | — | 是 | — |
| HTTP 5xx | 0 | — | — | 是 | — |
| 非 JSON / HTML | 0 | — | — | 视情况 | — |
| HTTP 403 | 0 | — | — | 暂停 | — |
| 站码缺失 | 0 | — | — | 否 | 三站均在 `station_name.js` 中命中 |
| 参数非法 | 0 | — | — | 否 | — |
| lease 提交失败 | 0 | — | — | 需诊断 | — |
| 数据库异常 | 0 | — | — | 需诊断 | — |

> 3 个请求全部成功，这张表全 0。**全 0 不是"没问题"的证据，而是"样本太小、还没碰到边界"的证据。** 真实风控通常出现在第几十个请求之后，3 个请求的成功率不能外推到 300 个。

## 10. 结果隔离检查

| 检查项 | 预期 | 结果 | 备注 |
| --- | --- | --- | --- |
| 是否写入 response 摘要 | 是 | **否** | 只写了 `row_count` 与 `response_hash`，`response_excerpt` 为空 |
| 是否写入对象存储 | 可选 | 否 | `object_uri` 为 `NULL` |
| 是否写入 `train_od_fare_edge` | 否 | 否 | `edge_count` 全 0 |
| 是否写入 `data/*.sqlite3` | 否 | 否 | — |
| 是否修改本地爬虫脚本 | 否 | 否 | — |
| 是否生成大批量任务 | 否 | 否 | 3 条 |

### 10.1 关键发现：采集结果没有落地

三条 response 记录的实际内容：

```text
task_id | status    | http | row_count | response_hash(前12位) | object_uri | response_excerpt
1       | succeeded | 200  | 52        | 1added6d6cc3          | NULL       | NULL
2       | succeeded | 200  | 73        | 4a78c7928be3          | NULL       | NULL
3       | succeeded | 200  | 335       | d8628a2a46a0          | NULL       | NULL
```

根因在 `backend/app/cloud_crawl/providers.py:36-40`：

```python
return ProviderRowsResult(
    rows=rows,                                  # ← 算完哈希后即被丢弃
    response_hash=hash_public_price_rows(rows),
    http_status=200,
)
```

**后果**：文档里阶段 6 的"离线合并"（把采集结果转成 `train_od_fare_edge` 基础边）当前没有输入数据。换句话说，这次 PoC 证明"能抓到"，但没有留下"抓到了什么"。

这不是实现错误，而是**设计尚未完成**：`object_uri` 字段本来就是为"原始响应放对象存储"预留的，只是还没有人往里写。云函数确实不该把大 JSON 塞进数据库，但也不能什么都不留。

## 11. 判定结论

```text
通过（有条件）
```

理由：

1. **通过的部分**：控制流全部验证通过——PostgreSQL 租约式认领、真实 HTTP 请求、错误分类、状态回写、幂等（`attempts=1` 无重试）、限速（1 秒间隔）、无死信、无超时。这是云函数最难验证、也最容易出问题的部分，一次全过。
2. **未满足的部分**：模板第 10 节要求"写入 response 摘要 = 是"，本次为否。实验产物为零，无法用于阶段 6 的合并验证。
3. 因此判定为"通过"仅指**控制流可用**，**不代表可以进入阶段 5 并发测绘**。按模板 12.1 的口径，通过后本应准备并发测绘；但本次的"结果留存"缺口优先级更高——先补落地，再谈并发。

## 12. 后续动作

### 12.1 立即执行（阻塞阶段 5）

1. ~~**让 payload 真正落库**~~ —— **已完成，见第 14 节**（job 2 验证通过）。
2. **用留下的数据走一次阶段 6 的离线合并**，产出 `train_od_fare_edge`。这是目前唯一还阻塞阶段 5 的事项。
3. 合并跑通后再决定：摘要截断长度是否够用（当前 8 KB），还是需要对象存储存全量。

### 12.2 随后

1. 修门禁脚本的默认模板路径（`BACKEND_ROOT.parent` 解析），避免每个新人都踩一次。
2. 回填任务表的 `from_station_code` / `to_station_code`，保证请求参数可复原。
3. job 收尾：本次 `crawl_cloud_job` id=1 结束时仍是 `running`，需要补一个"全部任务完成后置为 succeeded"的收尾逻辑。

### 12.3 暂不执行

- 不做并发测绘（阶段 5）。
- 不扩大样本。
- 不部署到云函数。

## 13. 附录

### 13.1 原始 summary

```json
{
  "job_id": 1,
  "worker_id": "local-worker-1",
  "started_at": "2026-11-23T12:12:33.652678Z",
  "finished_at": "2026-11-23T12:12:35.965818Z",
  "claimed_count": 3,
  "processed_count": 3,
  "succeeded_count": 3,
  "skipped_count": 0,
  "retry_wait_count": 0,
  "dead_letter_count": 0,
  "timeout_count": 0,
  "p50_latency_ms": 514,
  "p95_latency_ms": 807,
  "errors": []
}
```

### 13.2 关键日志摘录

```text
CloudPriceWorkerSummary(job_id=1, worker_id='local-worker-1',
  started_at=2026-11-23 12:12:33.652678Z, finished_at=2026-11-23 12:12:35.965818Z,
  claimed_count=3, processed_count=3, succeeded_count=3, skipped_count=0,
  retry_wait_count=0, dead_letter_count=0, timeout_count=0,
  p50_latency_ms=514, p95_latency_ms=807, errors=[])
```

### 13.3 dead-letter 样例

```text
task_id,from_station,to_station,error_type,error_message,http_status
（本次为空）
```

## 14. 补充执行：响应摘要落库（job 2）

第 10 节记录的缺口当场修掉，并在同一天用同一样本重跑验证。

### 14.1 改动

| 文件 | 改动 |
| --- | --- |
| `backend/app/cloud_crawl/worker.py` | `ProviderRowsResult` 新增 `response_excerpt: str \| None = None`；`_process_task` 把它带进 `CloudTaskResponseSummary` |
| `backend/app/cloud_crawl/providers.py` | 新增 `RESPONSE_EXCERPT_MAX_CHARS = 8000` 与 `build_response_excerpt(rows)`；`query_public_price` 返回值带上摘要 |
| `backend/tests/test_cloud_crawl_providers.py` | 新增 4 个测试：小样本不截断、大样本截断但保留总数、空行返回 None、provider 真的挂上摘要 |
| `backend/tests/test_cloud_crawl_worker.py` | 新增 `RecordingRepository` 与 1 个测试，验证 worker 把摘要透传到 `mark_succeeded` |

摘要格式（**统一信封，不存裸数组**）：

```json
{"total_rows": 52, "truncated": true, "rows": [ ... ]}
```

用信封的原因：裸数组被截断后，「335 行截成 40 行」和「本来就只返回 40 行」在数据上完全一样，事后无法分辨。信封里的 `total_rows` 让这一点永远可查。

落库前不需要改 schema——`crawl_cloud_station_od_response.response_excerpt` 这一列本来就有，`CloudTaskResponseSummary`、内存 repository、PostgreSQL repository 三层也早就在传这个字段。**缺的只是生产者（provider）从来没有填过它。**

### 14.2 重跑结果（job 2）

```text
CloudPriceWorkerSummary(job_id=2, worker_id='local-worker-2',
 started_at=2026-11-23 12:28:36.140199Z, finished_at=2026-11-23 12:28:38.481293Z,
 claimed_count=3, processed_count=3, succeeded_count=3, skipped_count=0,
 retry_wait_count=0, dead_letter_count=0, timeout_count=0,
 p50_latency_ms=634, p95_latency_ms=697, errors=[])
```

| task_id | row_count | response_excerpt 长度 | 是否截断 |
| ---: | ---: | ---: | --- |
| 4 | 52 | 7720 | 是 |
| 5 | 73 | 7726 | 是 |
| 6 | 335 | 7389 | 是 |

摘要开头（task 4，北京→上海）：

```json
{"total_rows":52,"truncated":true,"rows":[{"buttonTextInfo":"","queryLeftNewDTO":{"train_no":"240000G53106","station_train_code":"G531",...
```

三条都截断了（单条 OD 的原始 JSON 远超 8 KB），但 `total_rows` 完整、行内容可解析。**对阶段 6 的合并验证来说，几十行样本足够跑通链路**；真要全量留存，再上对象存储（`object_uri` 字段已预留）。

### 14.3 测试

`262 passed, 7 skipped, 1 warning`（基线 257 + 5 个新测试）。

### 14.4 对判定结论的影响

第 11 节的判定**不变**，理由变了：原来卡在"产物为零"，现在卡在"产物有了，但还没喂给阶段 6"。合并链路跑通之前，仍然不进入阶段 5。
