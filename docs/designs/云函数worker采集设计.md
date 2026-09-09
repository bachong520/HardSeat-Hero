# 云函数 worker 采集设计

## 1. 背景与目标

本文档定义相邻区间票价采集迁移到云函数 worker 的架构、运行约束和后续部署要求。当前本地 worker、handler、PostgreSQL repository 和真实 provider 适配已经实现；云厂商部署、调度、真实接口 PoC 和离线合并仍在后续阶段。

现有本地方案已经明确：

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

云函数方案的目标不是绕过任务粒度优化，也不是一次性 fan-out 所有请求，而是在已经完成 `unique station OD` 去重后，用可控、可恢复、可观测的 worker 队列提升采集吞吐。

核心目标：

- 定义中心任务表 schema。
- 定义 worker 输入输出格式。
- 定义任务领取、租约和幂等写入策略。
- 定义 retry / dead-letter 策略。
- 定义云函数并发测绘方案。
- 明确云函数实施约束与风险。

## 2. 当前实现状态与非目标\n\n当前已实现：\n\n- `backend/app/cloud_crawl/` 已包含任务模型、repository、retry、worker、handler、PostgreSQL repository、provider 适配和 schema。\n- 云函数相关离线测试已通过：`61 passed`。\n\n当前未完成：\n\n- 具体云厂商部署配置、调度、日志、监控和告警。\n- PostgreSQL 实机集成验证。\n- station OD 响应到 `train_od_fare_edge` 的离线 merge 流程。

当前已实现的本地能力如上；本文档仍不做以下事项：

1. 不把云函数作为 FastAPI 请求链路的一部分。
2. 不让 worker 直接写入 `train_od_fare_edge`。
3. 不设计反限流对抗、多云多出口或 IP 池方案。
3. 不改变在线搜索阶段只读本地静态库的原则。


## 3. 总体架构

推荐架构：

```text
本地/调度端生成 unique station OD 任务
  ↓
写入中心任务表
  ↓
云函数 worker 周期性或事件触发启动
  ↓
worker 小批量领取任务并设置 lease
  ↓
worker 按受控并发调用 queryAllPublicPrice
  ↓
结果写入中心结果表或对象存储
  ↓
离线合并流程写入 train_od_fare_edge
  ↓
失败任务进入 retry_wait 或 dead_letter
```

关键约束：

- 云函数 worker 不持有全量任务，只处理小批量任务。
- 中心任务表控制吞吐，而不是让云函数平台自由放大请求量。
- 任务状态更新必须带租约条件，避免重复提交覆盖正确状态。
- 结果写入必须幂等，因为云函数可能超时、重试或重复执行。

## 4. 任务粒度

云函数阶段任务粒度采用：

```text
unique station OD
```

即：

```text
(provider, crawl_date, from_station, to_station)
```

不采用：

```text
train OD
```

原因：`queryAllPublicPrice(from, to)` 返回该 station OD 下多趟车、多席别公布票价。若按 train OD 发任务，会重复请求同一个 station OD，放大请求量并增加限流风险。

对于一个 station OD，结果需要后续分发给本地已知的多个 train adjacent task：

```text
station OD price response
  ↓
按 train_no / train_code 匹配同组 train task
  ↓
展开 seat edge
  ↓
upsert train_od_fare_edge
```

## 5. 任务表 schema

云函数多实例并发写不建议使用 SQLite 作为中心任务库。推荐按 PostgreSQL / MySQL / 云数据库能力设计。字段避免绑定特定云厂商。

### 5.1 采集批次表

```sql
CREATE TABLE crawl_cloud_job (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    crawl_date DATE NOT NULL,
    job_type TEXT NOT NULL,
    task_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    total_count INTEGER NOT NULL DEFAULT 0,
    pending_count INTEGER NOT NULL DEFAULT 0,
    leased_count INTEGER NOT NULL DEFAULT 0,
    succeeded_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    dead_letter_count INTEGER NOT NULL DEFAULT 0,
    config_json TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL,
    last_error TEXT
);
```

建议取值：

```text
job_type: public_price_station_od
task_mode: adjacent_station_od | sampled_probe
status: pending | running | succeeded | failed | cancelled
```

### 5.2 station OD 任务表

```sql
CREATE TABLE crawl_cloud_station_od_task (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES crawl_cloud_job(id),
    provider TEXT NOT NULL,
    crawl_date DATE NOT NULL,
    from_station TEXT NOT NULL,
    to_station TEXT NOT NULL,
    from_station_code TEXT,
    to_station_code TEXT,
    priority INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_run_at TIMESTAMPTZ,
    leased_by TEXT,
    lease_token TEXT,
    lease_expires_at TIMESTAMPTZ,
    response_row_count INTEGER NOT NULL DEFAULT 0,
    matched_train_count INTEGER NOT NULL DEFAULT 0,
    edge_count INTEGER NOT NULL DEFAULT 0,
    last_error_type TEXT,
    last_error TEXT,
    last_http_status INTEGER,
    last_response_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    UNIQUE(job_id, crawl_date, from_station, to_station)
);

CREATE INDEX idx_crawl_cloud_station_od_pick
ON crawl_cloud_station_od_task(status, next_run_at, priority, id);

CREATE INDEX idx_crawl_cloud_station_od_lease
ON crawl_cloud_station_od_task(status, lease_expires_at);

CREATE INDEX idx_crawl_cloud_station_od_key
ON crawl_cloud_station_od_task(provider, crawl_date, from_station, to_station);
```

状态取值：

| 状态          | 含义                                           |
| ------------- | ---------------------------------------------- |
| `pending`     | 等待领取                                       |
| `leased`      | 已被某个 worker 领取，租约未过期               |
| `retry_wait`  | 等待下一次重试                                 |
| `succeeded`   | 请求成功且结果已保存                           |
| `skipped`     | 请求成功但业务上无需写入，例如该 OD 无匹配车次 |
| `failed`      | 达到可恢复失败终态，等待人工决定是否重置       |
| `dead_letter` | 不再自动重试，需要诊断                         |

### 5.3 原始响应表

建议保留 station OD 级别的原始响应摘要。完整响应可存对象存储，数据库只存定位字段。

```sql
CREATE TABLE crawl_cloud_station_od_response (
    job_id BIGINT NOT NULL,
    task_id BIGINT NOT NULL,
    provider TEXT NOT NULL,
    crawl_date DATE NOT NULL,
    from_station TEXT NOT NULL,
    to_station TEXT NOT NULL,
    status TEXT NOT NULL,
    http_status INTEGER,
    row_count INTEGER NOT NULL DEFAULT 0,
    response_hash TEXT NOT NULL,
    object_uri TEXT,
    response_excerpt TEXT,
    fetched_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(job_id, task_id, response_hash)
);
```

### 5.4 死信表

```sql
CREATE TABLE crawl_cloud_dead_letter (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL,
    task_id BIGINT NOT NULL,
    provider TEXT NOT NULL,
    crawl_date DATE NOT NULL,
    from_station TEXT NOT NULL,
    to_station TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    error_type TEXT NOT NULL,
    error_message TEXT NOT NULL,
    http_status INTEGER,
    response_excerpt TEXT,
    worker_id TEXT,
    created_at TIMESTAMPTZ NOT NULL
);
```

## 6. worker 输入格式

云函数启动输入应只包含运行控制参数，不直接塞入大量 OD 任务。

示例：

```json
{
  "job_id": 123,
  "provider": "full-snapshot-adjacent-cloud",
  "crawl_date": "2026-07-12",
  "worker_id": "cloud-price-worker-ap-shanghai-001",
  "batch_size": 20,
  "max_tasks_per_invocation": 100,
  "single_instance_concurrency": 2,
  "request_delay_ms": 200,
  "timeout_seconds": 60,
  "lease_seconds": 300,
  "dry_run": false
}
```

字段说明：

| 字段                          | 含义                              |
| ----------------------------- | --------------------------------- |
| `job_id`                      | 中心采集批次 ID                   |
| `provider`                    | 写入结果时使用的 provider 标识    |
| `crawl_date`                  | 采集日期                          |
| `worker_id`                   | worker 实例标识，用于日志和 lease |
| `batch_size`                  | 单次从任务表领取任务数            |
| `max_tasks_per_invocation`    | 单次云函数最多处理任务数          |
| `single_instance_concurrency` | 单实例内部并发请求数              |
| `request_delay_ms`            | 同实例内请求间隔                  |
| `timeout_seconds`             | 单请求超时                        |
| `lease_seconds`               | 任务租约时长                      |
| `dry_run`                     | 只领取和释放，不访问远程接口      |

## 7. worker 输出格式

云函数返回给调用方的摘要格式：

```json
{
  "job_id": 123,
  "worker_id": "cloud-price-worker-ap-shanghai-001",
  "started_at": "2026-07-02T06:00:00Z",
  "finished_at": "2026-07-02T06:04:30Z",
  "claimed_count": 40,
  "processed_count": 40,
  "succeeded_count": 35,
  "skipped_count": 1,
  "retry_wait_count": 3,
  "dead_letter_count": 1,
  "timeout_count": 2,
  "p50_latency_ms": 25000,
  "p95_latency_ms": 58000,
  "errors": [
    {
      "task_id": 456,
      "error_type": "timeout",
      "retryable": true,
      "message": "request timed out after 60s"
    }
  ]
}
```

该输出仅用于调度和观测。真实任务状态以中心任务表为准。

## 8. 任务领取与租约机制

worker 领取任务时必须原子更新状态：

```text
pending / retry_wait 且 next_run_at <= now
  ↓
leased
  ↓
设置 leased_by、lease_token、lease_expires_at、attempts = attempts + 1
```

PostgreSQL 可使用：

```sql
SELECT id
FROM crawl_cloud_station_od_task
WHERE job_id = :job_id
  AND status IN ('pending', 'retry_wait')
  AND (next_run_at IS NULL OR next_run_at <= now())
ORDER BY priority ASC, id ASC
FOR UPDATE SKIP LOCKED
LIMIT :batch_size;
```

然后在同一事务内更新为 `leased`。

状态提交必须携带 `lease_token`：

```sql
UPDATE crawl_cloud_station_od_task
SET status = :new_status,
    lease_token = NULL,
    leased_by = NULL,
    lease_expires_at = NULL,
    updated_at = now()
WHERE id = :task_id
  AND status = 'leased'
  AND lease_token = :lease_token;
```

如果更新影响行数为 0，说明租约已失效或任务已被其他 worker 回收，当前 worker 不应继续提交结果状态。

## 9. 幂等写入策略

### 9.1 任务幂等

任务表使用唯一键：

```text
(job_id, crawl_date, from_station, to_station)
```

重复生成任务时执行 upsert，不重复插入。

### 9.2 响应幂等

原始响应表使用：

```text
(job_id, task_id, response_hash)
```

同一响应重复提交不会产生重复记录。

### 9.3 基础边幂等

最终写入 `train_od_fare_edge` 时，唯一键应覆盖业务身份：

```text
(provider, train_no, from_station_no, to_station_no, seat_type)
```

如果后续决定保留采集日期维度，则使用：

```text
(provider, crawl_date, train_no, from_station_no, to_station_no, seat_type)
```

当前时间扩展图设计中，基础边不保存 `service_date`，只保存相对时刻和 day offset。因此需要谨慎决定 `provider` 是否编码采集批次，例如：

```text
provider = full-snapshot-adjacent-2026-07-12-cloud
```

否则不同日期的同车次基础边可能互相覆盖。

### 9.4 状态幂等

任务状态提交遵循：

```text
只有持有当前 lease_token 的 worker 可以提交状态
```

这可以避免：

- 过期 worker 覆盖新 worker 状态。
- 云函数超时后平台重试导致旧结果覆盖新结果。
- 任务回收后重复写状态。

## 10. retry / dead-letter 策略

### 10.1 错误分类

| 错误类型                    | 是否重试 | 处理                               |
| --------------------------- | -------: | ---------------------------------- |
| 网络连接失败                |       是 | 指数退避                           |
| 请求超时                    |       是 | 指数退避，必要时降低并发           |
| HTTP 5xx                    |       是 | 指数退避                           |
| HTTP 403 / 风控页           |   有条件 | 少量重试后 dead-letter，并触发降速 |
| 非 JSON / HTML 响应         |       是 | 视为可能风控或临时异常             |
| `status=false` 且信息不明确 |       是 | 指数退避                           |
| 站码缺失                    |       否 | dead-letter 或 failed              |
| 正常响应但无 rows           |       否 | skipped                            |
| 正常响应但无目标车次        |       否 | skipped，由后续匹配阶段处理        |
| 解析字段缺失                |   有条件 | 原始响应入库后 dead-letter         |

### 10.2 退避公式

建议：

```text
base_delay_seconds = 30
delay = min(3600, base_delay_seconds * 2 ** (attempts - 1))
jitter = random(0, delay * 0.2)
next_run_at = now + delay + jitter
```

默认最大重试：

```text
max_attempts = 5
```

超过最大次数后：

```text
retry_wait -> dead_letter
```

### 10.3 租约过期回收

调度端或维护任务定期执行：

```sql
UPDATE crawl_cloud_station_od_task
SET status = 'retry_wait',
    next_run_at = now(),
    leased_by = NULL,
    lease_token = NULL,
    lease_expires_at = NULL,
    updated_at = now(),
    last_error_type = 'lease_expired',
    last_error = 'worker lease expired before completion'
WHERE status = 'leased'
  AND lease_expires_at < now();
```

该逻辑必须存在，否则云函数超时会导致任务永久停在 `leased`。

### 10.4 dead-letter 重放

dead-letter 不自动删除。人工确认后可执行：

```text
dead_letter -> pending
attempts 重置或保留
next_run_at = now
```

重放前应先按 `error_type` 聚合检查，避免把风控类错误立即重新打爆。

## 11. 并发与限速模型

总吞吐由以下变量决定：

```text
总并发 = 云函数实例数 × 单实例并发
理论 RPS ≈ 总并发 / 单请求平均耗时
```

但实际应受全局限速约束：

```text
目标 RPS = min(测绘安全 RPS, 云函数理论 RPS, 数据库写入能力)
```

必须避免：

```text
一次性触发 150,000 个云函数调用
```

推荐做法：

- 调度器按固定频率触发少量 worker。
- 每个 worker 每次领取小批任务。
- 单实例并发从 1 或 2 开始。
- 发现错误率上升、p95 拉长、HTML/风控页增加时自动降速或暂停。

## 12. 并发测绘方案

### 12.1 测绘目标

测绘不是追求瞬时最大 RPS，而是确定稳定运行 profile：

- 成功率。
- p50 / p95 / p99 延迟。
- HTTP 错误率。
- 非 JSON / HTML 响应比例。
- 超时比例。
- dead-letter 比例。
- 每分钟有效成功任务数。
- 12306 是否出现明显风控行为。

### 12.2 测绘矩阵

从小到大逐级测试：

| 阶段 | 实例数 | 单实例并发 | delay | 样本数 |
| ---- | -----: | ---------: | ----: | -----: |
| P0   |      1 |          1 | 500ms |     50 |
| P1   |      1 |          2 | 300ms |    100 |
| P2   |      2 |          2 | 300ms |    200 |
| P3   |      4 |          2 | 200ms |    500 |
| P4   |      4 |          4 | 100ms |   1000 |
| P5   |      8 |          4 | 100ms |   2000 |

停止条件：

- 成功率低于 98%。
- p95 超过 90 秒。
- 非 JSON / HTML 响应比例超过 1%。
- HTTP 403 或疑似风控响应连续出现。
- dead-letter 比例超过 0.5%。

任一停止条件触发后，不继续提高并发，回退到上一档稳定 profile。

### 12.3 输出报告

每次测绘输出：

```json
{
  "profile": {
    "instance_count": 4,
    "single_instance_concurrency": 2,
    "request_delay_ms": 200,
    "timeout_seconds": 60
  },
  "sample_count": 500,
  "success_count": 493,
  "retryable_error_count": 6,
  "dead_letter_count": 1,
  "success_rate": 0.986,
  "p50_latency_ms": 26000,
  "p95_latency_ms": 62000,
  "p99_latency_ms": 88000,
  "effective_success_rps": 0.8,
  "html_response_count": 0,
  "http_403_count": 0
}
```

## 13. 云函数实施方案

### 13.1 推荐运行方式

第一版建议采用“调度触发 + 主动拉取任务”：

```text
云调度器每 N 秒触发 M 个 worker
  ↓
worker 从中心任务表领取 batch
  ↓
worker 处理并提交状态
```

不建议：

```text
每个任务发一条云队列消息，直接触发一个函数
```

原因：

- 容易产生不可控 fan-out。
- 单任务函数冷启动比例高。
- 全局限速难控制。
- 重试风暴风险更高。

### 13.2 云资源选择

需要的基础资源：

- 云函数运行时：Python。
- 中心数据库：PostgreSQL / MySQL / 云数据库。
- 对象存储：保存大响应或异常响应样本。
- 云调度器：周期性触发 worker。
- 日志服务：按 `job_id`、`worker_id`、`task_id` 查询日志。
- 告警：错误率、dead-letter、p95、风控响应比例。

### 13.3 环境变量

建议：

```text
CLOUD_CRAWL_DB_DSN
CLOUD_CRAWL_OBJECT_BUCKET
CLOUD_CRAWL_PROVIDER
CLOUD_CRAWL_DEFAULT_BATCH_SIZE
CLOUD_CRAWL_DEFAULT_CONCURRENCY
CLOUD_CRAWL_DEFAULT_DELAY_MS
CLOUD_CRAWL_TIMEOUT_SECONDS
CLOUD_CRAWL_LEASE_SECONDS
```

### 13.4 冷启动与超时

单次函数执行时间必须小于平台超时限制，并预留提交状态时间。

示例：

```text
云函数最大执行时间：300 秒
单请求 timeout：60 秒
单实例并发：2
batch_size：20
安全余量：30 秒
```

若剩余时间不足，应停止领取新任务并提交已完成结果。

### 13.5 出口与风控

需要明确记录：

- 云函数区域。
- 出口 IP 是否固定。
- 是否经 NAT 网关。
- 失败是否集中在某个区域或出口。
- TLS / Header / Cookie 行为是否与本地一致。

若云端成功率低于本地，不应继续扩容，应先定位出口、Header、Cookie、TLS 或风控差异。

## 14. 观测指标

必须记录以下指标：

| 指标                      | 用途                   |
| ------------------------- | ---------------------- |
| `claimed_count`           | 判断领取是否正常       |
| `succeeded_count`         | 有效吞吐               |
| `retry_wait_count`        | 临时失败压力           |
| `dead_letter_count`       | 需要人工诊断的失败     |
| `skipped_count`           | 正常无结果或无匹配任务 |
| `p50_latency_ms`          | 常规耗时               |
| `p95_latency_ms`          | 尾延迟                 |
| `timeout_count`           | 超时压力               |
| `html_response_count`     | 疑似风控或页面异常     |
| `http_403_count`          | 明确风控风险           |
| `lease_expired_count`     | 函数超时或崩溃风险     |
| `db_write_conflict_count` | 幂等或并发写问题       |

## 15. 风险与降级

### 15.1 主要风险

1. 12306 对云函数出口限流或风控更严格。
2. 云函数冷启动和执行超时放大失败率。
3. 中心数据库成为瓶颈。
4. lease 设计不严谨导致任务卡死或重复提交。
5. 过度并发导致 retry 风暴。
6. `provider` / 日期维度设计不清导致基础边互相覆盖。

### 15.2 降级策略

可执行降级：

- 降低实例数。
- 降低单实例并发。
- 增大 `request_delay_ms`。
- 暂停调度器。
- 只处理高优先级任务。
- 将 `retry_wait` 延迟拉长。
- 回退到本地长跑脚本。

### 15.3 暂停条件

满足任一条件应暂停云函数采集：

- 连续 5 分钟成功率低于 95%。
- HTML / 非 JSON 响应持续增加。
- HTTP 403 明显上升。
- p95 超过函数剩余可执行时间。
- dead-letter 快速增加且错误类型集中。

## 16. 实现复杂度估算

### 16.1 最小可用 PoC

```text
设计文档：0.5 天
任务表和 repository：1 天
worker 核心逻辑：1~1.5 天
本地模拟测试：0.5~1 天
云函数部署试跑：1 天
小规模测绘：1 天
合计：约 5~6 个工作日
```

PoC 目标：验证云函数能稳定访问 12306、小批量任务能领取和写回、retry/lease 基本可用。

### 16.2 可控生产化版本

```text
中心任务 schema 完整化：1~1.5 天
PostgreSQL/云数据库接入：1~2 天
worker 幂等写入和批处理：1.5~2 天
retry/dead-letter 完整实现：1 天
并发测绘脚本与指标输出：1~2 天
云函数部署流水线和配置文档：1 天
监控日志与采集报表：1 天
集成测试/故障注入：1~2 天
合计：约 9~13 个工作日
```

### 16.3 不建议当前实现的版本

多云、多区域、多出口 IP 调度版本预计需要 3~5 周以上，并且会显著提高运维和合规风险。当前阶段不建议进入该方向。

## 17. 验收标准\n\n### 17.1 已完成\n\n- 已定义任务表 schema、worker 输入输出、lease、幂等写入和 retry/dead-letter 策略。\n- 已完成内存 repository、worker、handler、provider 相关离线测试：`61 passed`。\n- 已实现真实 provider 适配和本地保守 profile 校验。\n\n### 17.2 待完成



- PostgreSQL repository 实机测试通过。\n- 云函数部署后可以使用真实中心库运行 handler。
- 已定义 worker 输入输出格式。
- 已定义 lease 领取和过期回收机制。
- 已定义幂等写入策略。
- 已定义 retry / dead-letter 策略。
- 已定义并发测绘矩阵和停止条件。
- 已说明云函数实施约束、风险和降级策略。

后续 PoC 验收：

- 单实例 worker 可处理小批 station OD。
- 云函数超时后任务可被 lease 回收。
- 重复执行不会重复写入结果。
- 可重试错误进入 `retry_wait`。
- 不可恢复错误进入 `dead_letter`。
- 并发测绘能输出成功率、p95、错误类型和有效 RPS。\n\n当前推荐顺序：先完成真实 provider 的 handler 装配和 PostgreSQL 实机验证，再选择云厂商并完成部署，最后执行 sampled probe PoC。
