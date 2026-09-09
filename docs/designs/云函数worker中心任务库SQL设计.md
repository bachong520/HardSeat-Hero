# 云函数 worker 中心任务库 SQL 设计

## 1. 背景与边界

本文补充 `docs/designs/云函数worker采集设计.md` 和 `docs/plans/云函数worker采集实施计划.md` 中尚未展开的中心任务库 SQL 设计，用于后续实现真实 PostgreSQL / MySQL repository。

当前车次时刻表长跑采集仍在进行，因此本文只做离线设计，不进入真实接口 PoC。

### 1.1 当前禁止事项

在本地长跑采集完成或明确确认不会冲突前，仍禁止：

1. 访问 `12306` 或运行任何联网采集脚本。
2. 运行本地爬虫脚本。
3. 写入 `data/*.sqlite3`。
4. 修改当前隔离文件：

   ```text
   backend/scripts/crawl_time_expanded_full_snapshot.py
   backend/tests/test_crawl_time_expanded_full_snapshot.py
   backend/scripts/compare_adjacent_vs_direct_price.py
   backend/tests/test_compare_adjacent_vs_direct_price.py
   data/*.sqlite3
   ```

5. 进入云函数阶段 4 真实接口小规模 PoC。
6. 生成大批量 station OD 任务。

### 1.2 本文目标

1. 固化中心任务库表结构。
2. 明确 PostgreSQL 与 MySQL 的任务领取差异。
3. 明确 `claim / lease / retry / dead-letter` 的 SQL 语义。
4. 明确幂等写入、唯一约束和索引设计。
5. 为后续真实 repository、migration 和云函数 PoC 提供实现依据。

### 1.3 非目标

本文不做：

1. 不实现 migration。
2. 不连接真实 PostgreSQL / MySQL。
3. 不替换当前内存 repository。
4. 不接入 FastAPI 在线搜索链路。
5. 不改变当前本地采集脚本。

## 2. 核心语义

云函数 worker 的中心任务库负责控制吞吐、恢复失败任务、保存结果摘要。任务粒度保持为：

```text
(job_id, provider, crawl_date, from_station, to_station)
```

即 `unique station OD`，不是 `train OD`。

状态机：

```text
pending
  -> leased
      -> succeeded
      -> skipped
      -> retry_wait
      -> dead_letter
retry_wait
  -> leased
leased 且 lease_expires_at < now
  -> retry_wait 或 pending
```

所有 worker 状态提交必须带 `lease_token`。没有当前 lease token 的提交必须失败，避免旧 worker、超时 worker 或重复执行覆盖正确状态。

## 3. 表结构

### 3.1 `crawl_cloud_job`

采集批次表。一个 job 对应一次受控采集批次，例如某日期的 sampled probe 或 adjacent station OD 采集。

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

CREATE INDEX idx_crawl_cloud_job_status
ON crawl_cloud_job(status, crawl_date, id);
```

建议取值：

| 字段        | 取值                                                     |
| ----------- | -------------------------------------------------------- |
| `job_type`  | `public_price_station_od`                                |
| `task_mode` | `sampled_probe`、`adjacent_station_od`                   |
| `status`    | `pending`、`running`、`succeeded`、`failed`、`cancelled` |

### 3.2 `crawl_cloud_station_od_task`

station OD 任务表。worker 只从该表领取小批量任务。

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

CREATE INDEX idx_crawl_cloud_station_od_job_status
ON crawl_cloud_station_od_task(job_id, status, id);
```

说明：

1. `UNIQUE(job_id, crawl_date, from_station, to_station)` 防止同一 job 内重复生成 station OD 任务。
2. `next_run_at IS NULL` 表示可立即运行。
3. `attempts` 在 claim 成功时递增，而不是在失败提交时递增。这样 worker 崩溃也会被计入一次领取尝试。
4. `lease_token` 每次 claim 必须生成新值。
5. `lease_expires_at` 到期后允许回收。

### 3.3 `crawl_cloud_station_od_response`

原始响应摘要表。完整响应可放对象存储，数据库只保存定位字段和摘要。

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

CREATE INDEX idx_crawl_cloud_response_key
ON crawl_cloud_station_od_response(provider, crawl_date, from_station, to_station);
```

幂等语义：

1. 同一响应重复写入不会产生重复记录。
2. response 写入成功后，再提交 task 终态。
3. 如果 response 已存在但 task 状态未提交，worker 可重试提交 task 终态。

### 3.4 `crawl_cloud_dead_letter`

死信表。记录不再自动重试的任务错误。

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

CREATE INDEX idx_crawl_cloud_dead_letter_job
ON crawl_cloud_dead_letter(job_id, created_at, id);

CREATE INDEX idx_crawl_cloud_dead_letter_key
ON crawl_cloud_dead_letter(provider, crawl_date, from_station, to_station);
```

## 4. PostgreSQL 实现策略

PostgreSQL 推荐使用 `FOR UPDATE SKIP LOCKED` 实现并发安全领取。

### 4.1 claim_tasks

语义：领取 `pending` 或已到期 `retry_wait` 任务，并设置 lease。

```sql
WITH picked AS (
    SELECT id
    FROM crawl_cloud_station_od_task
    WHERE job_id = :job_id
      AND status IN ('pending', 'retry_wait')
      AND (next_run_at IS NULL OR next_run_at <= :now)
    ORDER BY priority ASC, id ASC
    LIMIT :batch_size
    FOR UPDATE SKIP LOCKED
)
UPDATE crawl_cloud_station_od_task AS t
SET status = 'leased',
    leased_by = :worker_id,
    lease_token = :lease_token,
    lease_expires_at = :lease_expires_at,
    attempts = attempts + 1,
    updated_at = :now
FROM picked
WHERE t.id = picked.id
RETURNING t.*;
```

约束：

1. `:lease_token` 可以按任务生成，也可以按批次生成。推荐按任务生成，便于单任务提交和诊断。
2. 如果按任务生成，需要将 `picked` 结果在应用层循环更新，或使用数据库函数生成 token。
3. `batch_size` 必须有上限，不允许调用方无限扩大。
4. worker 领取到任务后只处理本批任务，不持有全量任务。

### 4.2 mark_succeeded

成功任务应先幂等写入 response，再提交终态。

```sql
INSERT INTO crawl_cloud_station_od_response (
    job_id,
    task_id,
    provider,
    crawl_date,
    from_station,
    to_station,
    status,
    http_status,
    row_count,
    response_hash,
    object_uri,
    response_excerpt,
    fetched_at
) VALUES (
    :job_id,
    :task_id,
    :provider,
    :crawl_date,
    :from_station,
    :to_station,
    'succeeded',
    :http_status,
    :row_count,
    :response_hash,
    :object_uri,
    :response_excerpt,
    :now
)
ON CONFLICT (job_id, task_id, response_hash) DO NOTHING;

UPDATE crawl_cloud_station_od_task
SET status = 'succeeded',
    response_row_count = :row_count,
    matched_train_count = :matched_train_count,
    edge_count = :edge_count,
    last_http_status = :http_status,
    last_response_hash = :response_hash,
    leased_by = NULL,
    lease_token = NULL,
    lease_expires_at = NULL,
    updated_at = :now,
    finished_at = :now
WHERE id = :task_id
  AND lease_token = :lease_token
  AND status = 'leased';
```

应用层必须检查第二条 `UPDATE` 的受影响行数：

- `1`：提交成功。
- `0`：lease 已失效、任务已被别人回收、重复提交或 token 错误，必须视为提交失败，不得盲目重试覆盖。

### 4.3 mark_skipped

请求成功但业务上不需要写边，例如该 station OD 无目标车次。

```sql
UPDATE crawl_cloud_station_od_task
SET status = 'skipped',
    response_row_count = :row_count,
    matched_train_count = 0,
    edge_count = 0,
    last_http_status = :http_status,
    last_response_hash = :response_hash,
    last_error_type = :reason_type,
    last_error = :reason,
    leased_by = NULL,
    lease_token = NULL,
    lease_expires_at = NULL,
    updated_at = :now,
    finished_at = :now
WHERE id = :task_id
  AND lease_token = :lease_token
  AND status = 'leased';
```

### 4.4 mark_retry_wait

可恢复错误进入 `retry_wait`。如果已经达到 `max_attempts`，应进入 `dead_letter`。

```sql
UPDATE crawl_cloud_station_od_task
SET status = CASE
        WHEN attempts >= max_attempts THEN 'dead_letter'
        ELSE 'retry_wait'
    END,
    next_run_at = CASE
        WHEN attempts >= max_attempts THEN NULL
        ELSE :next_run_at
    END,
    last_error_type = :error_type,
    last_error = :error_message,
    last_http_status = :http_status,
    leased_by = NULL,
    lease_token = NULL,
    lease_expires_at = NULL,
    updated_at = :now,
    finished_at = CASE
        WHEN attempts >= max_attempts THEN :now
        ELSE finished_at
    END
WHERE id = :task_id
  AND lease_token = :lease_token
  AND status = 'leased'
RETURNING status;
```

如果返回状态为 `dead_letter`，应用层应写入 `crawl_cloud_dead_letter`。也可以用同一事务完成：

```sql
INSERT INTO crawl_cloud_dead_letter (
    job_id,
    task_id,
    provider,
    crawl_date,
    from_station,
    to_station,
    attempts,
    error_type,
    error_message,
    http_status,
    response_excerpt,
    worker_id,
    created_at
)
SELECT
    job_id,
    id,
    provider,
    crawl_date,
    from_station,
    to_station,
    attempts,
    :error_type,
    :error_message,
    :http_status,
    :response_excerpt,
    :worker_id,
    :now
FROM crawl_cloud_station_od_task
WHERE id = :task_id
  AND status = 'dead_letter';
```

### 4.5 mark_dead_letter

不可恢复错误直接进入死信。

```sql
UPDATE crawl_cloud_station_od_task
SET status = 'dead_letter',
    last_error_type = :error_type,
    last_error = :error_message,
    last_http_status = :http_status,
    leased_by = NULL,
    lease_token = NULL,
    lease_expires_at = NULL,
    updated_at = :now,
    finished_at = :now
WHERE id = :task_id
  AND lease_token = :lease_token
  AND status = 'leased';
```

随后在同一事务写入 `crawl_cloud_dead_letter`。

### 4.6 reclaim_expired_leases

用于回收 worker 崩溃、云函数超时、进程被杀导致的过期 lease。

```sql
UPDATE crawl_cloud_station_od_task
SET status = CASE
        WHEN attempts >= max_attempts THEN 'dead_letter'
        ELSE 'retry_wait'
    END,
    next_run_at = CASE
        WHEN attempts >= max_attempts THEN NULL
        ELSE :next_run_at
    END,
    last_error_type = 'lease_expired',
    last_error = 'lease expired before worker committed final status',
    leased_by = NULL,
    lease_token = NULL,
    lease_expires_at = NULL,
    updated_at = :now,
    finished_at = CASE
        WHEN attempts >= max_attempts THEN :now
        ELSE finished_at
    END
WHERE status = 'leased'
  AND lease_expires_at < :now
RETURNING *;
```

是否立即进入 `pending` 或 `retry_wait`：

- 推荐进入 `retry_wait`，并设置短退避，避免崩溃任务被立即反复领取。
- 对 dry-run 释放任务可单独走 `release_task()`，不使用过期 lease 回收逻辑。

## 5. MySQL 实现策略

### 5.1 MySQL 8.0.1+ 可用 `SKIP LOCKED`

MySQL 8.0.1+ 支持 `FOR UPDATE SKIP LOCKED`。可在事务中先选 id，再更新。

```sql
START TRANSACTION;

SELECT id
FROM crawl_cloud_station_od_task
WHERE job_id = ?
  AND status IN ('pending', 'retry_wait')
  AND (next_run_at IS NULL OR next_run_at <= ?)
ORDER BY priority ASC, id ASC
LIMIT ?
FOR UPDATE SKIP LOCKED;

UPDATE crawl_cloud_station_od_task
SET status = 'leased',
    leased_by = ?,
    lease_token = ?,
    lease_expires_at = ?,
    attempts = attempts + 1,
    updated_at = ?
WHERE id IN (...selected ids...);

COMMIT;
```

注意：MySQL 没有 PostgreSQL 的 `UPDATE ... RETURNING` 通用能力。应用层通常需要：

1. 事务内 `SELECT id ... FOR UPDATE SKIP LOCKED`。
2. 事务内 `UPDATE ... WHERE id IN (...)`。
3. 事务内或提交后重新 `SELECT * WHERE id IN (...)`。

### 5.2 无 `SKIP LOCKED` 的替代策略

如果目标云数据库不支持 `SKIP LOCKED`，使用两阶段条件更新，牺牲部分并发效率。

第一阶段选择候选 id：

```sql
SELECT id
FROM crawl_cloud_station_od_task
WHERE job_id = ?
  AND status IN ('pending', 'retry_wait')
  AND (next_run_at IS NULL OR next_run_at <= ?)
ORDER BY priority ASC, id ASC
LIMIT ?;
```

第二阶段带条件更新，只有仍处于可领取状态的行才会被 claim：

```sql
UPDATE crawl_cloud_station_od_task
SET status = 'leased',
    leased_by = ?,
    lease_token = ?,
    lease_expires_at = ?,
    attempts = attempts + 1,
    updated_at = ?
WHERE id IN (...candidate ids...)
  AND status IN ('pending', 'retry_wait')
  AND (next_run_at IS NULL OR next_run_at <= ?);
```

第三阶段读取本 worker 成功领取的任务：

```sql
SELECT *
FROM crawl_cloud_station_od_task
WHERE leased_by = ?
  AND lease_token = ?
  AND status = 'leased';
```

限制：

1. 多 worker 同时选到相同候选 id 时，只有一个能条件更新成功。
2. 应用层必须以实际读取到的 leased 任务为准。
3. 如果使用批次级 `lease_token`，同一 worker 同一时刻不要并行发起多个 claim，否则读取结果可能混淆。
4. 更安全的做法是每个任务独立 token，或 claim 请求携带 `claim_id` 字段；当前表未设计 `claim_id`，后续如需要可扩展。

## 6. Repository 方法到 SQL 映射

| Repository 方法                                             | SQL 语义                                                              | 必须检查                                                           |
| ----------------------------------------------------------- | --------------------------------------------------------------------- | ------------------------------------------------------------------ |
| `claim_tasks(job_id, batch_size, worker_id, lease_seconds)` | 选取可运行任务，设置 `leased` 和 lease 字段，递增 `attempts`          | 返回任务数不得超过 `batch_size`；不得领取未到 `next_run_at` 的任务 |
| `mark_succeeded(task_id, lease_token, response_summary)`    | 幂等写 response，任务进入 `succeeded`                                 | `UPDATE` 受影响行数必须为 1                                        |
| `mark_skipped(task_id, lease_token, reason)`                | 任务进入 `skipped`                                                    | 必须带当前 `lease_token`                                           |
| `mark_retry_wait(task_id, lease_token, error)`              | 可恢复错误进入 `retry_wait`，达到 `max_attempts` 时进入 `dead_letter` | 必须计算 `next_run_at`；必须检查是否转死信                         |
| `mark_dead_letter(task_id, lease_token, error)`             | 不可恢复错误进入 `dead_letter` 并写死信表                             | 必须同事务写 task 和 dead letter                                   |
| `reclaim_expired_leases(now)`                               | 回收过期 `leased` 任务                                                | 不得回收未过期 lease                                               |

## 7. 幂等与事务边界

### 7.1 任务生成幂等

任务生成使用唯一键：

```text
UNIQUE(job_id, crawl_date, from_station, to_station)
```

PostgreSQL：

```sql
INSERT INTO crawl_cloud_station_od_task (...)
VALUES (...)
ON CONFLICT (job_id, crawl_date, from_station, to_station) DO NOTHING;
```

MySQL：

```sql
INSERT IGNORE INTO crawl_cloud_station_od_task (...)
VALUES (...);
```

或：

```sql
INSERT INTO crawl_cloud_station_od_task (...)
VALUES (...)
ON DUPLICATE KEY UPDATE updated_at = updated_at;
```

### 7.2 结果写入幂等

response 使用主键：

```text
PRIMARY KEY(job_id, task_id, response_hash)
```

重复执行同一任务、重复提交同一响应，不应产生重复 response。

### 7.3 状态提交事务

推荐事务顺序：

```text
BEGIN
  INSERT response ON CONFLICT DO NOTHING
  UPDATE task WHERE id = ? AND lease_token = ? AND status = 'leased'
COMMIT
```

若 `UPDATE task` 失败，说明 lease 不再有效。此时 response 可能已插入。该情况可接受，因为 response 是幂等摘要；后续诊断可发现 response 存在但 task 终态未提交。若要严格避免孤立 response，可将 response 写入放到 `UPDATE task RETURNING` 成功之后，但会降低故障恢复可观测性。

## 8. retry 与 backoff

`retry_wait` 的 `next_run_at` 由应用层根据错误类型和 attempts 计算。建议沿用当前 `backend/app/cloud_crawl/retry.py` 的有界指数退避语义。

推荐策略：

| 错误类型            | 状态                    | 说明                   |
| ------------------- | ----------------------- | ---------------------- |
| 网络超时            | `retry_wait`            | 退避后重试             |
| HTTP 5xx            | `retry_wait`            | 退避后重试             |
| 非 JSON / HTML      | `retry_wait` 或暂停 job | 若比例升高，应停止 PoC |
| HTTP 403            | `retry_wait` 并触发暂停 | 不应继续升并发         |
| 站码缺失            | `dead_letter`           | 通常不可恢复           |
| 参数非法            | `dead_letter`           | 任务构造错误           |
| 达到 `max_attempts` | `dead_letter`           | 不再自动重试           |

## 9. job 计数维护

`crawl_cloud_job` 中的计数字段有两种维护方式。

### 9.1 实时增量维护

每次 task 状态变化时同步更新 job 计数。

优点：查询快。

缺点：事务更复杂，状态迁移必须严谨处理旧状态和新状态。

### 9.2 查询时聚合

通过 task 表聚合：

```sql
SELECT status, COUNT(*)
FROM crawl_cloud_station_od_task
WHERE job_id = :job_id
GROUP BY status;
```

优点：实现简单，不容易计数漂移。

缺点：大 job 查询成本更高。

建议第一版真实 repository 使用查询时聚合；job 表计数字段作为缓存或后续优化，不作为强一致来源。

## 10. 暂停与取消

阶段 4 前必须具备暂停调度器或停止 worker 的手段。

数据库层建议支持 job 状态：

```text
pending | running | succeeded | failed | cancelled
```

worker claim 前必须检查 job：

```sql
SELECT status
FROM crawl_cloud_job
WHERE id = :job_id;
```

只有 `pending` 或 `running` 可领取任务。若 job 为 `cancelled`、`failed`、`succeeded`，worker 不应 claim 新任务。

可选优化：将 job 状态检查合并进 claim SQL：

```sql
AND EXISTS (
    SELECT 1
    FROM crawl_cloud_job j
    WHERE j.id = t.job_id
      AND j.status IN ('pending', 'running')
)
```

## 11. 实现门禁

实现真实 repository 前必须满足：

1. 明确目标数据库：PostgreSQL、MySQL 或具体云数据库。
2. 明确 migration 工具。
3. 阶段 1~3 离线测试继续通过。
4. 仍不访问 `12306`。
5. 仍不写 `data/*.sqlite3`。
6. 不修改当前本地采集脚本。

进入阶段 4 真实接口小规模 PoC 前还必须满足：

1. 当前本地长跑采集已完成，或明确确认不会与云端请求冲突。
2. 中心任务库可用。
3. claim、lease、retry、dead-letter 已有真实 repository 测试。
4. 暂停机制可用。
5. 只准备 sampled probe 小样本任务。
6. profile 固定，不临时提高 batch、并发或降低 delay。
7. 已有 HTML/403、成功率、p95、dead-letter 停止条件。

## 12. 后续任务

建议后续按以下顺序推进：

1. 选择目标数据库和 migration 工具。
2. 基于本文编写 migration。
3. 新增真实 `CloudCrawlRepository` 实现。
4. 用测试数据库验证：
   - 并发 claim 不重复领取。
   - 错误 lease token 不能提交。
   - 过期 lease 可回收。
   - response 重复写入幂等。
   - 达到 `max_attempts` 进入 dead-letter。
5. 准备阶段 4 PoC 报告模板。
6. 等本地采集完成后，再评估是否进入小规模真实接口 PoC。