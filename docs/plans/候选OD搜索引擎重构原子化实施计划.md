# 在线搜索引擎重构原子化实施计划

## 目标

将 `backend/app/services/route_search.py` 中的 provider-backed 搜索重构为长期意义上的在线搜索引擎，与离线图搜索引擎形成清晰边界：

```text
RouteSearchEngine
  ├─ OnlineRouteSearchEngine
  │    当前阶段：在线 OD 搜索，依赖 provider.search_segments() 懒加载边
  │    当前策略：候选站扩展、请求预算、缓存、遥测排序
  │
  └─ OfflineGraphRouteSearchEngine
       当前实现：TimeExpandedRouteSearchEngine
       当前策略：在线只读本地静态图或 SQLite 快照
```

本计划只处理在线搜索引擎重构。目标是：

- 对外只暴露 `source`、`status`、`stream_snapshots(query)`。
- `stream_snapshots(query)` 是唯一搜索入口，返回 `AsyncIterator[StreamSnapshot]`。
- 内部删除“`max_transfers < 2` 走旧一换逻辑、`max_transfers >= 2` 走 `CheapestPathSearcher`”这种结构性分支。
- 使用一个统一的在线路径搜索流程覆盖直达、一换、二换。
- `max_transfers` 只作为路径约束，不作为算法分派条件。
- 当前候选站逻辑降级为在线扩展策略，不再作为长期 engine 命名。

## 两条路线边界

### OnlineRouteSearchEngine

定位：强依赖公共接口或 provider 的在线搜索。

允许：

- 用户查询链路中调用 `TrainDataProvider.search_segments()`。
- 使用远程 fallback。
- 使用请求预算、并发限制、缓存、遥测排序。
- 在预算内返回不完整但有诊断信息的结果。

必须做到：

- 明确暴露搜索范围、预算、缓存命中、失败 OD、剪枝诊断。
- 不宣称全国全局最低价。
- 公共接口调用必须受预算和并发控制。
- 搜索结构必须统一，不按换乘次数切换算法。

### OfflineGraphRouteSearchEngine

定位：弱依赖实时接口的本地静态图搜索。

禁止：

- 用户查询链路中调用 12306 或其他公共接口。
- 在图搜索过程中做远程 fallback。

必须做到：

- 在线阶段只读本地数据库或预构建图。
- 搜索结果可复现。
- 暴露数据版本、更新时间、覆盖率和缺边诊断。
- 后续可升级为 Dijkstra、label-setting 或 Pareto 多标签搜索。

## 非目标

- 不在本计划内重写 `TimeExpandedRouteSearchEngine`。
- 不改变 `RouteQuery.max_transfers` 的领域含义。
- 不引入新的远程接口。
- 不把历史最低价作为 A* hard lower bound。
- 不承诺在线搜索具备全国全局最低价正确性。
- 不在同一切片内混入前端 UI 改造。

## 目标结构

在线搜索引擎目标形态：

```python
class OnlineRouteSearchEngine:
    def __init__(self, provider: TrainDataProvider, ...): ...

    @property
    def source(self) -> str: ...

    @property
    def status(self) -> dict[str, object]: ...

    async def stream_snapshots(self, query: RouteQuery) -> AsyncIterator[StreamSnapshot]:
        context = OnlineSearchContext()
        started_at = perf_counter()
        plans = await self._search_plans(query, context)
        await self._finish_search(query, plans, context)
        yield self._build_snapshot(query, plans, context, started_at, final=True)
```

内部搜索目标：

```text
RouteQuery
  ↓
构造在线扩展策略：终点、provider 候选站、遥测命中候选、后续可扩展的显式中转/战略通道
  ↓
统一在线路径搜索：状态 = 当前站、已选 segments、累计价格、到达时间、已访问站
  ↓
通过 provider.search_segments() 懒加载 OD 边，受缓存、预算、并发控制约束
  ↓
找到到达终点的路径后构造 TransferPlan
  ↓
排序、截断、记录遥测、产出 final snapshot
```

## 命名决策

长期引擎名称使用在线/离线语义，不使用 `candidate` 作为引擎名。

- `OnlineRouteSearchEngine` -> `OnlineRouteSearchEngine`
- `CheapestPathSearcher` -> 删除或内联为 `_search_plans()`
- `PathSearchState` -> `OnlineSearchState`
- `ParetoFrontier` -> `OnlineSearchParetoFrontier`，如保留
- `SearchContext` -> `OnlineSearchContext`
- `BestPriceTracker` -> 删除；统一搜索流程不再需要一换并发任务间共享上界
- `_ranked_transfer_candidates()` 后续可改为 `_ranked_expansion_stations()` 或移动到策略对象

工厂兼容名：

```text
ROUTE_SEARCH_ENGINE 未设置 / online / candidate / od-candidate / legacy
  -> OnlineRouteSearchEngine

ROUTE_SEARCH_ENGINE=offline-graph / time-expanded / time_expanded
  -> TimeExpandedRouteSearchEngine
```

`status["search_engine"]` 建议返回：

```python
"search_engine": "online",
"online_remote_io": True,
"global_optimum_guaranteed": False,
```

当前候选站策略可通过独立字段表达：

```python
"expansion_strategy": "candidate-station-od"
```

## 切片 1：建立在线引擎命名和协议同形态，不改行为

### 修改文件

- `backend/app/services/route_search.py`
- `backend/app/services/route_search_engine_factory.py`
- `backend/tests/test_route_search.py`
- `backend/tests/test_route_search_engine_factory.py`
- `docs/architecture/项目整体架构.md`

### 实施内容

1. 将 `OnlineRouteSearchEngine` 重命名为 `OnlineRouteSearchEngine`。
2. 工厂返回新类。
3. 工厂新增正式值 `ROUTE_SEARCH_ENGINE=online`。
4. 保留 `candidate`、`od-candidate`、`legacy` 作为兼容别名，全部映射到 `OnlineRouteSearchEngine`。
5. 测试 import 和构造调用同步更新。
6. 暂时保留内部 `_iter_plans()` 和 `CheapestPathSearcher`，只完成对外命名收敛。
7. 文档中明确：`candidate` 是历史别名，不是长期架构概念。

### 验证

```powershell
backend\.venv\Scripts\python.exe -m pytest backend\tests\test_route_search.py backend\tests\test_route_search_engine_factory.py -q
```

## 切片 2：收敛 `stream_snapshots()` 为唯一主流程

### 修改文件

- `backend/app/services/route_search.py`
- `backend/tests/test_route_search.py`

### 实施内容

1. 让 `stream_snapshots(query)` 变成清晰的顶层流程：
   - 创建 `OnlineSearchContext`。
   - 调用 `_search_plans(query, context)`。
   - 去重、排序、截断。
   - `_finish_search()`。
   - yield 一个 `final=True` snapshot。
2. 暂时不保留中间 snapshot 增量更新，先与 `TimeExpandedRouteSearchEngine` 对齐。
3. 删除 `_iter_plans()` 作为对外搜索主干的地位；如果暂时需要，可作为 `_legacy_search_plans()` 被 `_search_plans()` 调用，但必须标记为临时。

### 验证

- 所有测试必须改为读取最终 snapshot。
- 删除或调整依赖“中间 snapshot 替换 Top plans”的测试，因为目标结构是和 time-expanded 一样的最终 snapshot。

```powershell
backend\.venv\Scripts\python.exe -m pytest backend\tests\test_route_search.py backend\tests\test_api.py -q
```

## 切片 3：引入统一在线路径搜索，但保留旧测试语义

### 修改文件

- `backend/app/services/route_search.py`
- `backend/tests/test_route_search.py`

### 实施内容

1. 新增 `_search_plans(query, context)`，覆盖 `max_transfers=0/1/2`。
2. 使用单一优先队列：

```python
queue: list[tuple[Decimal, int, OnlineSearchState]]
```

3. 初始状态为起点、空 segment、价格 0、无到达时间。
4. 每次从队列弹出状态后，下一站集合统一由 `_next_expansion_stations(state, query, expansion_stations)` 生成：
   - 永远包含终点，用于直达或当前站直达终点。
   - 如果 `state.transfer_count < query.max_transfers`，再包含未访问扩展站。
5. 对每个 `next_station` 调用 `_candidate_segments(...)` 获取可乘 OD 边；此函数后续可改名为 `_load_online_segments(...)`。
6. 生成 `next_state` 或到达终点后生成 `TransferPlan`。
7. 保留 `ParetoFrontier` 和 best known complete price 剪枝，但只在统一搜索内使用。
8. `_heuristic_lower_bound()` 保持 `0` 或直接删除启发函数命名，优先队列只按累计价格排序。

### 重要约束

- `max_transfers=0` 时，扩展只能尝试终点，因此等价直达。
- `max_transfers=1` 时，统一搜索自然允许一换，不再走专用 `_iter_one_transfer()`。
- `max_transfers=2` 时，统一搜索自然允许二换。
- 不再根据 `max_transfers >= 2` 切换算法。

### 验证

重点测试：

- 直达最低价。
- 一换最低价。
- 二换最低价。
- `max_transfers=1` 不返回二换。
- 最短换乘时间过滤。
- 最大总耗时过滤。
- 同价排序。
- provider 单个 OD 失败不炸整次搜索。
- 请求预算不超限。

```powershell
backend\.venv\Scripts\python.exe -m pytest backend\tests\test_route_search.py -q
```

## 切片 4：删除旧一换专用路径和并发任务结构

### 修改文件

- `backend/app/services/route_search.py`
- `backend/tests/test_route_search.py`
- `docs/architecture/项目整体架构.md`

### 删除内容

- `_iter_plans()` 中的 `max_transfers` 分支。
- `_iter_one_transfer()`。
- `_one_transfer_plans_for_station()`。
- `BestPriceTracker`。
- “一换搜索一次性创建所有候选站任务”的相关测试。

### 保留内容

- `_ranked_transfer_candidates()`：当前在线扩展排序仍有价值，后续改名或迁入策略对象。
- `_search_segments()`：缓存、预算、并发限制仍是在线引擎核心边界。
- `_search_transfer_window_segments()`：跨日窗口仍有价值，但命名可后续调整。
- `_priced_segments_sorted_by_departure()`。
- `_plan_sort_key()`。
- `_finish_search()` 和 telemetry。

### 验证

```powershell
backend\.venv\Scripts\python.exe -m pytest backend\tests\test_route_search.py backend\tests\test_api.py backend\tests\test_route_search_engine_factory.py -q
```

## 切片 5：清理命名和诊断字段

### 修改文件

- `backend/app/services/route_search.py`
- `backend/tests/test_route_search.py`
- `docs/architecture/项目整体架构.md`
- `docs/logs/YYYY-MM-DD.md`

### 实施内容

1. `SearchDiagnostics.expanded_candidates` 改名为 `expanded_stations` 或 `expanded_od_targets`。
2. `failed_candidates` 改名为 `failed_od_targets`。
3. `pruned_by_best_price_count` 改名为 `pruned_by_complete_price_bound_count`，避免误读为数学下界。
4. `status` 中增加：

```python
"search_engine": "online",
"expansion_strategy": "candidate-station-od",
"online_remote_io": True,
"global_optimum_guaranteed": False,
```

5. 文档明确：online 当前阶段是预算内在线 OD 搜索，不是完整铁路图搜索。

### 验证

```powershell
backend\.venv\Scripts\python.exe -m pytest backend\tests\test_route_search.py backend\tests\test_api.py -q
```

## 切片 6：文档回填和历史表述修正

### 修改文件

- `docs/architecture/项目整体架构.md`
- `docs/designs/时间扩展图搜索方案.md`
- `docs/logs/YYYY-MM-DD.md`

### 实施内容

1. 删除架构文档中“一换搜索 / 二换搜索”作为核心结构的章节。
2. 改为：

```text
OnlineRouteSearchEngine
  -> 统一在线路径搜索
  -> max_transfers 只作为路径约束，不作为算法分派条件
  -> 当前扩展策略是 candidate-station-od
```

3. 明确 online 与 offline graph 的差异：
   - online：在线懒查 provider，受候选策略、缓存、预算限制。
   - offline graph：本地静态图搜索，在线不访问远程 provider。
4. 记录迁移原因：旧结构来自历史兼容，现已清理。

### 验证

文档检查即可，无需新增代码测试。

## 最终验收命令

```powershell
backend\.venv\Scripts\python.exe -m pytest backend\tests -q
pnpm --dir frontend build
```

## 完成标准

- `route_search.py` 中不存在 `if query.max_transfers >= 2` 这种算法分派。
- 在线搜索引擎只有一个主搜索入口：`stream_snapshots(query)`。
- `max_transfers` 只作为路径约束参与状态扩展。
- 类名、状态名、诊断名都表达 online search，不把 `candidate` 固化为长期引擎概念。
- `candidate`、`od-candidate`、`legacy` 只作为工厂兼容别名保留。
- 工厂、API、测试仍能通过现有 `RouteSearchEngine` 协议调用。
- 文档明确 online 的正确性边界和 offline graph 的长期方向。