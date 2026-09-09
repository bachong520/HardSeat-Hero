# 云函数 worker 采集实施计划

## 1. 目标

基于 `docs/designs/云函数worker采集设计.md`，为后续将相邻区间票价采集迁移到云函数 worker 制定可执行实施计划。

本计划已进入实现后的收敛阶段。阶段 1~3 的代码、测试和本地脚本已经落地；阶段 4~6 仍需推进。

## 2. 当前上下文

现有本地采集链路已经形成以下顺序：

```text
getTrainName(date)
  ↓
queryTrainInfo/query 获取真实经停表
  ↓
从经停表派生相邻 station OD
  ↓
按 (from_station, to_station) 去重
  ↓
queryAllPublicPrice 查询公布票价
  ↓
匹配多个 train task 并写入 train_od_fare_edge
```

云函数 worker 的定位是后续扩展，不替代当前正在运行的本地采集进程，也不进入 FastAPI 在线搜索链路。

## 3. 非目标

本计划明确不做以下事项：

1. 不修改当前本地爬虫脚本。
2. 不运行本地爬虫、测试或任何会访问 12306 的命令。
3. 不写入 `data/*.sqlite3`。
4. 正式云厂商部署配置、调度和监控仍待完成。
5. 正式 migration 工具尚未确定，当前以 `postgres_schema.sql` 和初始化脚本为准。
6. 不把云函数 worker 接入 FastAPI 请求链路。
7. 不设计 IP 池、多云多出口或反限流对抗方案。

## 4. 当前状态

当前不再存在原先设定的“本地长跑期间禁止推进云函数”的项目门禁。代码和文档可以继续更新；真实接口仍应采用小样本、低并发、带延迟的 profile，并保留可暂停和结果隔离措施。

### 4.1 已落地代码\n\n`backend/app/cloud_crawl/`、本地 worker 脚本、PostgreSQL schema/repository 和相关测试已经完成阶段 1~3 的主要交付。

```text
backend/scripts/crawl_time_expanded_full_snapshot.py
backend/tests/test_crawl_time_expanded_full_snapshot.py
backend/scripts/compare_adjacent_vs_direct_price.py
backend/tests/test_compare_adjacent_vs_direct_price.py
data/*.sqlite3
```

### 4.2 当前不再适用的历史限制\n\n原先针对本地长跑采集设置的文件修改、测试执行和联网请求限制已解除；真实 provider 仍必须按 sampled_probe profile 执行。

```text
pytest
python backend/scripts/crawl_time_expanded_full_snapshot.py
python backend/scripts/compare_adjacent_vs_direct_price.py
任何访问 12306 的脚本
任何写入 SQLite 静态库的脚本
```

### 4.3 原因

- 避免污染当前长跑采集的工作区状态。
- 避免占用或改写 SQLite 运行时数据。
- 避免并发请求放大 12306 访问压力。
- 避免在 P0/P1/P2 本地采集未稳定前引入云端变量。

## 5. 总体实施策略

采用分阶段推进：

```text
文档计划
  ↓
中心任务模型设计
  ↓
本地 mock worker
  ↓
云函数 handler 薄适配
  ↓
真实接口小规模 PoC
  ↓
并发测绘
  ↓
离线合并到 train_od_fare_edge
```

关键原则：

- 云 worker 只处理 `unique station OD`。
- 中心任务表控制吞吐，不让云函数平台自由 fan-out。
- worker 先写原始响应摘要或对象存储，不直接写 `train_od_fare_edge`。
- 最终基础边写入通过离线 merge 流程完成。
- 所有状态提交必须带 `lease_token`。
- 所有结果写入必须幂等。

## 6. 阶段拆分

### 阶段 0：计划与边界固化（已完成）

#### 目标

在不干扰当前爬虫的前提下，明确后续云函数 worker 的实施边界和阶段顺序。

#### 交付物

- `docs/plans/云函数worker采集实施计划.md`
- `docs/logs/开发日志.md` 追加记录

#### 验收标准

- 明确禁止修改的爬虫文件和数据库文件。
- 明确禁止执行的联网、测试和写库命令。
- 明确后续实现只能从独立模块开始。

### 阶段 1：中心任务模型设计落地准备（已完成）

#### 目标

将设计文档中的 schema 转为后续可实现的数据库模型和 repository 约定。

#### 建议新增模块

```text
backend/app/cloud_crawl/
  models.py
  repository.py
  retry.py
```

#### 任务

1. 定义 `CloudCrawlJob`、`CloudStationOdTask`、`CloudStationOdResponse`、`CloudDeadLetter` 数据结构。
2. 定义任务状态枚举：

   ```text
   pending | leased | retry_wait | succeeded | skipped | failed | dead_letter
   ```

3. 定义 repository 接口：

   ```text
   claim_tasks(job_id, batch_size, worker_id, lease_seconds)
   mark_succeeded(task_id, lease_token, response_summary)
   mark_skipped(task_id, lease_token, reason)
   mark_retry_wait(task_id, lease_token, error)
   mark_dead_letter(task_id, lease_token, error)
   reclaim_expired_leases(now)
   ```

4. 补充 PostgreSQL 与 MySQL 的任务领取差异。
5. 明确 `FOR UPDATE SKIP LOCKED` 不可用时的替代策略。

#### 单元测试任务

- claim 只领取 `pending/retry_wait` 且 `next_run_at <= now` 的任务。
- claim 会设置 `leased_by`、`lease_token`、`lease_expires_at` 并递增 `attempts`。
- 非当前 `lease_token` 不能提交成功状态。
- 过期 lease 可被回收到 `retry_wait`。
- 重复生成同一 `(job_id, crawl_date, from_station, to_station)` 不产生重复任务。

### 阶段 2：本地 mock worker（已完成）

#### 目标

在本地用 mock provider 验证 worker 状态机，不访问 12306。

#### 建议新增模块

```text
backend/app/cloud_crawl/worker.py
backend/app/cloud_crawl/metrics.py
backend/scripts/run_cloud_price_worker_local.py
```

#### 任务

1. 定义 worker 输入配置：

   ```text
   job_id
   provider
   crawl_date
   worker_id
   batch_size
   max_tasks_per_invocation
   single_instance_concurrency
   request_delay_ms
   timeout_seconds
   lease_seconds
   dry_run
   ```

2. 定义 worker 输出摘要：

   ```text
   claimed_count
   processed_count
   succeeded_count
   skipped_count
   retry_wait_count
   dead_letter_count
   timeout_count
   p50_latency_ms
   p95_latency_ms
   errors
   ```

3. 实现 dry-run：只领取和释放，不访问 provider。
4. 实现 mock provider：返回固定 rows、空 rows、可重试错误、不可恢复错误。
5. 实现 retry/backoff 计算函数。

#### 单元测试任务

- 成功响应进入 `succeeded`。
- 空 rows 进入 `skipped`。
- 网络超时进入 `retry_wait` 并设置 `next_run_at`。
- 站码缺失进入 `dead_letter`。
- 达到 `max_attempts` 后进入 `dead_letter`。
- `dry_run=true` 时不调用 provider。
- `max_tasks_per_invocation` 能限制单次处理量。

### 阶段 3：云函数 handler 薄适配（已完成，需补 provider 装配）

#### 目标

新增云函数入口，但核心逻辑仍保留在 `backend/app/cloud_crawl/worker.py`，避免云厂商绑定。

#### 建议新增模块

```text
backend/app/cloud_crawl/cloud_function_handler.py
```

#### 任务

1. 从 event 和环境变量解析 worker 配置。
2. 校验 `job_id`、`provider`、`crawl_date`、并发和 batch 参数。
3. 调用通用 worker。
4. 返回 worker summary。
5. 预留剩余执行时间检查接口。

#### 单元测试任务

- event 参数覆盖环境变量默认值。
- 缺少必填字段时返回受控错误。
- `dry_run=true` 能正常返回 summary。
- 剩余时间不足时停止领取新任务。

### 阶段 4：真实接口小规模 PoC

#### 前置条件

必须满足以下条件后才能进入：

- 当前本地长跑采集已完成，或确认不会与云端请求冲突。
- 中心任务库可用。
- mock worker 状态机测试通过。
- retry、lease、dead-letter 可用。
- 已有暂停调度器的手段。

#### 执行前检查清单

阶段 4 可以开始。以下检查用于控制执行风险和记录环境准备情况，不再是阻止代码和文档推进的项目门禁。

| 检查项       | 必须满足的条件                                                              | 当前不满足时的处理                   |
| ------------ | --------------------------------------------------------------------------- | ------------------------------------ |
| 本地采集状态 | 当前本地长跑采集已结束，或明确确认云端请求不会与本地采集冲突                | 不启动真实接口 PoC                   |
| 外部请求压力 | 当前没有其他 12306 高频采集任务运行                                         | 不触发云 worker，不运行联网脚本      |
| 中心任务库   | PostgreSQL/MySQL/云数据库任务表可用，并已验证 claim/lease/retry/dead-letter | 继续使用内存 repository 做离线测试   |
| 暂停机制     | 已有明确的暂停调度器或停止 worker 触发方式                                  | 不进入云端运行                       |
| mock 验证    | 阶段 1~3 的 repository、worker、handler 离线测试通过                        | 修复本地状态机后再评估               |
| 初始任务集   | 只准备 sampled_probe 小样本任务，且任务量受控                               | 不生成大批量 station OD 任务         |
| profile 固化 | 使用阶段 4 初始 profile，不提高并发                                         | 禁止临时调高 batch、并发或降低 delay |
| 停止条件     | 已明确 HTML/403、成功率、p95、dead-letter 的停止条件                        | 不执行 PoC                           |
| 结果隔离     | worker 只写响应摘要或对象存储，不写 `train_od_fare_edge`                    | 不接入基础边写入                     |
| 日志记录     | 已准备记录 profile、样本数、成功率、错误类型、p95、有效 RPS                 | 不执行 PoC                           |

当前阶段可以继续进行阶段 4 的环境准备、真实 provider 小样本验证和云函数部署适配，但仍不得直接跳到大规模并发。

- 阶段 1~3 的代码质量收敛。
- handler 参数校验和 summary 序列化测试。
- repository SQL 方案设计文档：`docs/designs/云函数worker中心任务库SQL设计.md`。
- 阶段 4 门禁和 PoC 报告模板文档：`docs/plans/云函数worker真实接口PoC报告模板.md`。
- 不访问网络、不写 SQLite、不修改本地采集脚本的纯单元测试。

当前已补充的离线文档产物：

- `docs/designs/云函数worker中心任务库SQL设计.md`：细化 PostgreSQL/MySQL 中心任务库表结构、claim、lease、retry、dead-letter、幂等写入和实现门禁。
- `docs/plans/云函数worker真实接口PoC报告模板.md`：固化阶段 4 真实接口 PoC 的门禁 checklist、初始 profile、停止条件、指标汇总、错误分类和结果隔离检查。

当前已补充的 PostgreSQL 中心任务库实现产物：

- `backend/app/cloud_crawl/postgres_schema.sql`：PostgreSQL 中心任务库 schema。
- `backend/app/cloud_crawl/postgres_repository.py`：真实 `PostgresCloudCrawlRepository`，覆盖 claim、lease token 终态提交、retry、dead-letter、response 幂等写入和过期 lease 回收。
- `backend/scripts/init_cloud_crawl_postgres.py`：schema 初始化脚本。
- `backend/app/cloud_crawl/sample_tasks.py`：sampled_probe 小样本任务生成逻辑，支持去重、限量和可复现顺序。
- `backend/scripts/prepare_cloud_crawl_sampled_probe.py`：sampled_probe 任务准备脚本；默认只预览，不写库，不访问 12306。
- `backend/tests/test_cloud_crawl_postgres_repository.py`：PostgreSQL repository 集成测试；未设置 `CLOUD_CRAWL_TEST_DATABASE_URL` 时自动跳过。
- `backend/tests/test_cloud_crawl_sample_tasks.py`：sampled_probe 任务生成单元测试。
- `backend/app/cloud_crawl/cloud_function_handler.py`：支持通过 `CLOUD_CRAWL_DATABASE_URL` 创建 PostgreSQL repository；未设置时仍使用内存 repository。

阶段 4 当前剩余工作：

1. 在真实 PostgreSQL 测试库设置 `CLOUD_CRAWL_TEST_DATABASE_URL` 并跑通 `backend/tests/test_cloud_crawl_postgres_repository.py`。
2. 使用 `backend/scripts/prepare_cloud_crawl_sampled_probe.py` 准备 sampled_probe 小样本任务，不生成大批量 station OD。
3. 确认暂停 worker 的触发方式。
4. 接入真实 provider 前保持初始 profile，不临时升 batch、并发或降低 delay。
5. 完成 PoC 报告模板中的门禁 checklist 后，才运行真实接口。

#### 初始 profile

```text
task_mode=sampled_probe
batch_size=5
max_tasks_per_invocation=10
single_instance_concurrency=1
request_delay_ms=1000
timeout_seconds=60
lease_seconds=300
```

#### 验收标准

- 单实例 worker 可处理小批 station OD。
- 任务可从 `pending` 进入 `leased`，再进入终态。
- 云函数超时或模拟崩溃后，lease 可回收。
- 重复执行不重复写响应摘要。
- 可重试错误进入 `retry_wait`。
- 不可恢复错误进入 `dead_letter`。

### 阶段 5：并发测绘

#### 目标

确定云端稳定运行 profile，不追求瞬时最大 RPS。

#### 测绘矩阵

| 阶段 | 实例数 | 单实例并发 | delay | 样本数 |
| ---- | -----: | ---------: | ----: | -----: |
| P0   |      1 |          1 | 500ms |     50 |
| P1   |      1 |          2 | 300ms |    100 |
| P2   |      2 |          2 | 300ms |    200 |
| P3   |      4 |          2 | 200ms |    500 |
| P4   |      4 |          4 | 100ms |   1000 |
| P5   |      8 |          4 | 100ms |   2000 |

#### 停止条件

满足任一条件立即停止升档：

- 成功率低于 98%。
- p95 超过 90 秒。
- 非 JSON / HTML 响应比例超过 1%。
- HTTP 403 或疑似风控响应连续出现。
- dead-letter 比例超过 0.5%。

#### 输出报告

每轮测绘必须输出：

```text
instance_count
single_instance_concurrency
request_delay_ms
timeout_seconds
sample_count
success_count
retryable_error_count
dead_letter_count
success_rate
p50_latency_ms
p95_latency_ms
p99_latency_ms
effective_success_rps
html_response_count
http_403_count
```

### 阶段 6：离线合并到基础边

#### 目标

将云 worker 保存的 station OD 原始响应离线合并到 `train_od_fare_edge`。

#### 原则

- worker 不直接写 `train_od_fare_edge`。
- merge 流程依赖本地已完成的时刻表快照和 train adjacent task。
- 按 `train_no` / `train_code` 匹配 rows。
- 最终写入仍走幂等 upsert。

#### provider 命名

建议编码采集批次：

```text
full-snapshot-adjacent-2026-07-12-cloud
```

避免复用泛化 provider：

```text
12306-public-price
full-snapshot
```

原因：`train_od_fare_edge` 当前基础边不保存 `service_date`，不同日期同车次基础边可能互相覆盖。

#### 测试任务

- 同一 response 重复 merge 不重复写边。
- 同一 station OD 可分发给多个 train adjacent task。
- 无目标车次 rows 不写边并记录 skipped。
- 不同 provider 不互相覆盖。

## 7. 风险与控制

| 风险                       | 控制措施                                                     |
| -------------------------- | ------------------------------------------------------------ |
| 云函数出口被限流或风控     | 小样本 PoC，逐级测绘，发现 HTML/403 立即暂停                 |
| retry 风暴                 | 中心任务表限速，指数退避，max_attempts，暂停调度器           |
| lease 设计错误导致任务卡死 | 实现 lease 回收任务和故障注入测试                            |
| 云 worker 直接污染基础边   | 第一版只写响应表或对象存储，离线 merge                       |
| provider 覆盖历史基础边    | provider 编码采集日期和 cloud 标识                           |
| 数据库成为瓶颈             | 小 batch claim，索引覆盖 pick/lease/key 查询，记录写冲突指标 |

## 8. 验收总标准

后续完整实现完成时应满足：

- 中心任务表 schema 已实现并有 migration。
- worker 输入输出格式稳定。
- pending/retry_wait 领取、lease 提交、lease 回收均有测试。
- retry/dead-letter 策略有测试。
- dry-run 不访问远程接口。
- mock worker 状态机测试通过。
- 小规模真实 PoC 可输出成功率、p95、错误类型和有效 RPS。
- 云 worker 不直接接入 FastAPI 在线搜索链路。
- 云 worker 不直接写 `train_od_fare_edge`，由离线 merge 完成。

## 9. 当前可执行结论

当前阶段 1~3 已完成，下一步应从 PostgreSQL 实机准备和 handler provider 装配开始；随后再进入具体云厂商部署和真实接口 sampled probe。阶段 5 并发测绘及阶段 6 离线 merge 不应提前于阶段 4 的结果验证。