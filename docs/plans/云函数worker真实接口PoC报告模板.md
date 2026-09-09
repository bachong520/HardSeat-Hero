# 云函数 worker 真实接口 PoC 报告模板

## 1. 使用边界

本文是阶段 4 真实接口小规模 PoC 的报告模板。当前已允许执行 PoC，但必须按小样本、保守 profile 和可暂停原则执行。

PoC 不应执行以下动作：

1. 不访问 `12306`。
2. 不运行云函数 worker 真实 provider。
3. 不运行本地爬虫脚本。
4. 不写入 `data/*.sqlite3`。
5. 不生成大批量 station OD 任务。
6. 不写入 `train_od_fare_edge`。

使用本文记录真实 PoC 前，应完成 PostgreSQL 测试库验证、云函数部署配置检查和 sampled_probe 任务准备。

## 2. PoC 基本信息

| 项目            | 内容                                         |
| --------------- | -------------------------------------------- |
| PoC 编号        | `cloud-worker-poc-YYYYMMDD-001`              |
| 执行日期        |                                              |
| 执行人          |                                              |
| 目标环境        | 本地 / 云函数 / 其他                         |
| 云厂商与区域    |                                              |
| job_id          |                                              |
| provider        |                                              |
| crawl_date      |                                              |
| task_mode       | `sampled_probe`                              |
| 中心库 DSN 来源 | `CLOUD_CRAWL_DATABASE_URL`，不得记录敏感密钥 |
| repository 实现 | `PostgresCloudCrawlRepository`               |
| 样本来源        |                                              |
| 样本任务数      |                                              |
| 是否写基础边    | 必须为否                                     |
| 是否写 SQLite   | 必须为否                                     |

## 3. 执行前检查清单

检查结果用于决定是否执行和记录风险；发现关键项失败时应暂停 PoC。

| 检查项                        | 必须满足                                                                                   | 结果 | 备注 |
| ----------------------------- | ------------------------------------------------------------------------------------------ | ---- | ---- |
| 本地采集状态                  | 本地长跑采集已结束，或确认不会与云端请求冲突                                               |      |      |
| 外部请求压力                  | 当前没有其他 12306 高频采集任务运行                                                        |      |      |
| 中心任务库                    | PostgreSQL/MySQL/云数据库任务表可用                                                        |      |      |
| claim/lease/retry/dead-letter | 真实 repository 已验证                                                                     |      |      |
| PostgreSQL 集成测试           | `CLOUD_CRAWL_TEST_DATABASE_URL` 对应测试库已通过 `test_cloud_crawl_postgres_repository.py` |      |      |
| 暂停机制                      | 可立即停止调度器或 worker                                                                  |      |      |
| 样本任务                      | 只使用 sampled_probe 小样本                                                                |      |      |
| profile 固化                  | 不临时提高并发、不降低 delay                                                               |      |      |
| 停止条件                      | HTML/403、成功率、p95、dead-letter 条件已确认                                              |      |      |
| 结果隔离                      | 只写响应摘要或对象存储，不写基础边                                                         |      |      |
| 日志记录                      | 已准备记录本模板要求的指标                                                                 |      |      |

执行结论：

```text
允许执行 / 暂停执行
```

如果结论为“暂停执行”，停止填写运行结果，先修复环境或配置问题。

## 4. 固定运行 profile

初始 profile 必须保守，不允许临时升档。

```text
task_mode=sampled_probe
batch_size=5
max_tasks_per_invocation=10
single_instance_concurrency=1
request_delay_ms=1000
timeout_seconds=60
lease_seconds=300
```

本次实际 profile：

| 参数                          |   值 | 是否符合初始 profile | 备注         |
| ----------------------------- | ---: | -------------------- | ------------ |
| `batch_size`                  |      |                      |              |
| `max_tasks_per_invocation`    |      |                      |              |
| `single_instance_concurrency` |      |                      |              |
| `request_delay_ms`            |      |                      |              |
| `timeout_seconds`             |      |                      |              |
| `lease_seconds`               |      |                      |              |
| 云函数实例数                  |      |                      | 初始必须为 1 |

## 5. 样本任务说明

样本任务必须小、可解释、可复现。

| 字段            | 内容                                                   |
| --------------- | ------------------------------------------------------ |
| 样本生成方式    |                                                        |
| 样本 OD 数      |                                                        |
| 车站范围        |                                                        |
| 是否包含热门 OD |                                                        |
| 是否包含边界 OD |                                                        |
| 是否去重        |                                                        |
| 是否可复现      |                                                        |
| 生成脚本        | `backend/scripts/prepare_cloud_crawl_sampled_probe.py` |
| 是否先预览      | 必须为是                                               |

样本清单摘要：

```text
from_station,to_station,from_station_code,to_station_code
...
```

## 6. 停止条件

满足任一条件必须立即停止，不允许继续升并发或扩大样本。

| 条件                |                        阈值 | 实际值 | 是否触发 | 处理动作 |
| ------------------- | --------------------------: | -----: | -------- | -------- |
| 成功率              |                     `< 98%` |        |          |          |
| p95 latency         |                     `> 90s` |        |          |          |
| 非 JSON / HTML 比例 |                      `> 1%` |        |          |          |
| HTTP 403            |              连续出现即停止 |        |          |          |
| dead-letter 比例    |                    `> 0.5%` |        |          |          |
| 云函数超时          |      任一批次超时需暂停分析 |        |          |          |
| 中心库异常          | claim/commit 异常需暂停分析 |        |          |          |

停止结论：

```text
未触发停止 / 已触发停止
```

若已触发停止，必须记录停止时间、原因和后续处置。

## 7. 执行记录

### 7.1 启动命令或触发方式

```text
记录云函数触发参数、环境变量或本地受控命令。
不得记录敏感密钥。
```

### 7.2 时间线

| 时间 | 事件             | 备注 |
| ---- | ---------------- | ---- |
|      | 开始执行         |      |
|      | 首批 claim       |      |
|      | 首个成功响应     |      |
|      | 首个 retry_wait  |      |
|      | 首个 dead_letter |      |
|      | 停止执行         |      |

## 8. 指标汇总

| 指标                    |   值 |
| ----------------------- | ---: |
| `sample_count`          |      |
| `claimed_count`         |      |
| `processed_count`       |      |
| `succeeded_count`       |      |
| `skipped_count`         |      |
| `retry_wait_count`      |      |
| `dead_letter_count`     |      |
| `timeout_count`         |      |
| `success_rate`          |      |
| `p50_latency_ms`        |      |
| `p95_latency_ms`        |      |
| `p99_latency_ms`        |      |
| `effective_success_rps` |      |
| `html_response_count`   |      |
| `http_403_count`        |      |
| `non_json_count`        |      |
| `provider_error_count`  |      |
| `db_claim_error_count`  |      |
| `db_commit_error_count` |      |

## 9. 错误分类

| 错误类型       | 数量 | 示例 task_id | 示例 OD | 是否可重试 | 处理结论 |
| -------------- | ---: | ------------ | ------- | ---------- | -------- |
| 网络超时       |      |              |         | 是         |          |
| HTTP 5xx       |      |              |         | 是         |          |
| 非 JSON / HTML |      |              |         | 视情况     |          |
| HTTP 403       |      |              |         | 暂停       |          |
| 站码缺失       |      |              |         | 否         |          |
| 参数非法       |      |              |         | 否         |          |
| lease 提交失败 |      |              |         | 需诊断     |          |
| 数据库异常     |      |              |         | 需诊断     |          |

## 10. 结果隔离检查

| 检查项                        | 预期 | 结果 | 备注 |
| ----------------------------- | ---- | ---- | ---- |
| 是否写入 response 摘要        | 是   |      |      |
| 是否写入对象存储              | 可选 |      |      |
| 是否写入 `train_od_fare_edge` | 否   |      |      |
| 是否写入 `data/*.sqlite3`     | 否   |      |      |
| 是否修改本地爬虫脚本          | 否   |      |      |
| 是否生成大批量任务            | 否   |      |      |

## 11. 判定结论

PoC 结论只能选择一个：

```text
通过 / 不通过 / 中止 / 无效
```

判定标准：

| 结论   | 条件                                                              |
| ------ | ----------------------------------------------------------------- |
| 通过   | 样本任务完成，成功率、p95、HTML/403、dead-letter 均未触发停止条件 |
| 不通过 | 未触发强制中止，但核心指标不满足继续测绘要求                      |
| 中止   | 触发停止条件或人工暂停                                            |
| 无效   | 样本、配置、日志或结果隔离不满足要求，数据不可用于决策            |

最终结论：

```text

```

理由：

```text

```

## 12. 后续动作

根据结论选择后续动作。

### 12.1 若通过

不得直接大规模运行。下一步只能准备阶段 5 并发测绘，并保持逐级升档。

待办：

1. 固化本次 profile。
2. 记录安全样本范围。
3. 准备 P0/P1 测绘，不跳级。
4. 继续保持结果隔离，不写基础边。

### 12.2 若不通过或中止

待办：

1. 保留任务库状态和响应摘要。
2. 导出错误分类。
3. 暂停重试风控类任务，直到完成诊断。
4. 修复 repository、worker 或 provider 后重新执行检查清单。

### 12.3 若无效

待办：

1. 标记本次报告无效。
2. 不使用本次指标做并发决策。
3. 修正样本、profile 或日志采集后再评估。

## 13. 附录

### 13.1 原始 summary

```json
{
  "claimed_count": 0,
  "processed_count": 0,
  "succeeded_count": 0,
  "skipped_count": 0,
  "retry_wait_count": 0,
  "dead_letter_count": 0,
  "timeout_count": 0,
  "p50_latency_ms": null,
  "p95_latency_ms": null,
  "errors": []
}
```

### 13.2 关键日志摘录

```text

```

### 13.3 dead-letter 样例

```text
task_id,from_station,to_station,error_type,error_message,http_status
```