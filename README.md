# 林火卫星热点 · 林班归属明细服务

把卫星热点按林班归属整理成可入库的明细 CSV，供林火数据仓库按责任区汇总。

技术栈：FastAPI + SQLite（WAL）+ Shapely/GEOS。分布式一致性不依赖 Redis 或消息队列：
两个 Web 实例共用一个 SQLite 文件，版本切换与结果发布都在 `BEGIN IMMEDIATE` 事务内完成。

---

## 快速启动

```bash
docker compose up --build
```

| 实例 | 地址 |
| --- | --- |
| web-a | http://localhost:8080 |
| web-b | http://localhost:8081 |

两个实例共享卷 `forest-data`，访问任意一个都能看到同一份数据；两个都内置后台计算 worker。

本机无 Docker 时：

```bash
python3 -m pip install -r requirements.txt
FOREST_DB_PATH=./data/forest.db uvicorn app.main:app --port 8000
```

### 样例数据（`samples/`）

| 文件 | 说明 |
| --- | --- |
| `compartments_v1.geojson` | 5 个林班；A-01/B-02/C-03 共边，E-05 与 A-01 重叠 |
| `compartments_v2.geojson` | 调整后的底图版本，用于演示"换图后重算同批热点" |
| `hotspots.csv` | 15 条热点，含共边点、角点、重叠区点、区外点、重复坐标的独立来源行 |

推荐演示流程：

1. 上传 `compartments_v1.geojson` → 记录底图 v1。
2. 上传 `hotspots.csv` → 自动计算并发布，页面显示 7 已赋码 / 6 冲突 / 2 未匹配。
3. 上传 `compartments_v2.geojson` → 底图变为 v2；此时页面仍展示并允许下载 v1 的结果，并有醒目提示。
4. 点"按当前底图重算"（或"当前底图重算全部批次"）→ 新结果发布为 v2 明细。
5. 打开两个浏览器窗口分别指向 8080/8081，重复步骤 3~4，可见两个实例共用同一队列与发布状态。
6. 打开 <http://localhost:8080/manifests>，全部批次就绪后点"冻结当前全部批次为清单"；
   再上传新热点批次或换图重算，回看该清单：标为"已过期"，但条目 CSV 仍是冻结时的内容。

---

## 归属规则

对每条热点做精确点面判断（GEOS `covers` 包含边界，`contains` 不含边界），
候选几何来自 STRtree 包围盒查询后再精确判定，结果与要素顺序无关。

| 情况 | 归属状态 | 原因类型 | 明细中的林班号 |
| --- | --- | --- | --- |
| 只落入一个林班内部 | 已赋码 | 唯一落入 | 该林班 |
| 只落在唯一林班边界上 | 已赋码（含边界） | 边界赋码 | 该林班 |
| 落在多个林班的公共边/角点 | 归属冲突 | 共边 | 空，候选写入"候选林班号" |
| 至少落入一个林班内部且落在多个林班 | 归属冲突 | 重叠区 | 空，候选写入"候选林班号" |
| 不在任何林班内 | 未匹配 | 区外 | 空 |

关键点：

- **共边不是"先到先得"**。热点正好压在 A-01 与 B-02 的公共边上时，两个多边形都 `covers` 它，
  因此判为冲突，候选 `A-01;B-02` 按林班号排序，与文件内要素顺序无关。
- **每条来源都保留**。热点的 `seq` 是来源行序号，不做去重/合并；同一坐标出现两次就是两行，
  分别赋码。CSV 额外带"来源行号"（CSV 中的物理行）便于回溯。
- **重叠区 vs 共边**的区分基于"是否至少有一个多边形在内部包含该点"，便于数据治理时区分
  "面拓扑本身有问题"与"点被放在线上"。

CSV 列：`来源文件, 来源行号, 热点号, 经度, 纬度, UTC时刻, 归属状态, 原因类型, 原因说明,
林班号, 候选林班号, 林班底图版本, 计算记录号`。使用 UTF-8 BOM + CRLF，Excel 直接打开不乱码。

---

## 输入格式

### 热点 CSV

必需列（表头大小写/空格/下划线不敏感，支持中文别名）：

| 字段 | 可接受表头 |
| --- | --- |
| 热点号 | `hotspot_no` / `热点号` / `热点编号` / `hotspot_id` |
| 经度 | `lon` / `lng` / `longitude` / `经度` |
| 纬度 | `lat` / `latitude` / `纬度` |
| UTC 时刻 | `observed_at` / `utc` / `time` / `timestamp` / `时刻` / `UTC时刻` |

- 额外列原样保留在数据库（`hotspots.raw_json`），当前不导出。
- 时刻会规范化为 `YYYY-MM-DDTHH:MM:SSZ`；带偏移的会换算到 UTC（如 `+08:00`）。
  **无时区的值按 UTC 解释**——列定义本身是 UTC，不做本地时区猜测。
- 坏数据整批拒绝并给出中文行号提示（经度越界、热点号为空、时刻无法解析、列数多于表头）。

### 林班 GeoJSON

- `Feature` 或 `FeatureCollection`；几何必须是面（Polygon/MultiPolygon，允许 GeometryCollection 内部为面）。
- 林班号属性键：`code` / `林班号` / `林班` / `compartment` / `compartment_code` / `compartment_no`。
- 林班号必须唯一，否则拒绝（避免仓库汇总时出现歧义）。
- 无效几何自动 `make_valid` 修复，修复数量记录在底图版本上并在页面提示。
- 经纬度范围、空几何、非面几何、缺林班号都会明确报错且**不改变当前底图**（校验先于写入事务）。

---

## 一致性与并发保证

`app/service.py` 的模块注释是权威说明，这里是要点：

1. **底图版本不可变**。上传新图 = 插入新的 `map_versions` 行 + 一组新的 `compartments` 行，
   旧版本只被标记为非活动，从不原地修改。部分唯一索引
   `CREATE UNIQUE INDEX ... ON map_versions(is_active) WHERE is_active = 1`
   保证任何时刻只有一个活动版本。
2. **计算记录绑定版本**。每个 run 在创建时固定 `map_version_id`，计算过程读的是该版本的快照。
3. **发布是受保护的比较-交换**。发布事务内重新读取活动版本、批次已发布 run，并校验本次运行
   仍持有该 run 的执行令牌（attempt token），三者同时满足才写入 `published_run_id`。
   - 计算期间底图被换掉 → run 置为 `superseded`，不会发布，并自动按新底图补排队一次。
   - 旧计算晚于新计算完成 → 新 run id 更大，旧 run 不会覆盖 `published_run_id`。
   - 两个实例同时发布 → SQLite 写锁串行化，只有先到者成功，后者看到更新的 run 后退出。
4. **失败保留上一份结果**。只有成功发布才会改 `batches.published_run_id`；失败/作废的 run
   绝不改动它，因此"下载 CSV"始终返回上一份成功明细。失败 run 的临时结果行会被清理。
5. **下载是单快照**。下载时先锁定该批次的 `published_run_id`，随后只读该 run 的结果行；
   即使下载过程中另一实例发布了新结果，也不会出现新旧混合的行。
6. **崩溃恢复**。运行中的 run 每 5 秒写心跳；worker 发现心跳超过 `FOREST_STALE_RUN_SECONDS`
   的 run 会重新排队。此时旧 worker 若复活，其写操作因 attempt token 不匹配而被丢弃，
   不会把新一次计算误标为失败或清掉半成品。心跳阈值需大于单批热点最大计算时长。

### 并发测试

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest                      # 单元 + API 测试
python3 -m pytest -m slow              # 真起两个 uvicorn 进程共享同一 SQLite 的并发测试
```

`tests/test_concurrency.py` 覆盖：两实例并发换图后只有一份活动底图、任一实例触发的重算都发布到
同一活动版本、最终 CSV 全部行来自同一版本且无重复/缺失；以及一边换图重算、一边创建供数清单时，
成功创建的清单内部版本一致、此后仅标过期而条目 CSV 字节不变。

---

## 供数清单（仓库按多批热点取数）

数据员按批次分批取数时，逐批下载会拼入不同时点的结果。供数清单把**同一底图下全部批次的
已发布归属**冻结成一组，供一次交付：

- **创建** `POST /manifests`（页面 `/manifests`）。检查当时**全部批次**是否都已发布**当前底图**
  的归属；缺失、待算、失败的批次会逐条列出且**拒绝生成**，清单与条目均不写入。
- **冻结内容**：清单记录创建时间、底图版本（含 sha256）、批次数与汇总计数；每个条目固定
  `batch_id + run_id + map_version_id + 行数/计数`。页面 `/manifests/{id}` 逐项给出
  `/manifests/{id}/items/{batch_id}.csv`。
- **原子性**：就绪检查与清单写入在同一个 `BEGIN IMMEDIATE` 事务内完成。两个 Web 实例交错
  上传批次、换图、发布计算时，清单要么反映该事务前已提交的完整状态，要么根本不生成；
  不会出现半新半旧的条目。
- **过期**：查看时实时比对当前活动底图、当前批次集合与各批次当前的 `published_run_id`；
  任一变化即在页面与 `/api/manifests/{id}` 标注过期并列出原因。
- **旧清单不失效**：过期只是标记。条目 CSV 始终按**清单冻结的 run_id** 读取该计算的结果行，
  不读 `batches.published_run_id`；此后新批次、新发布、甚至失败重算都不改动旧清单及其 CSV。
  清单条目通过外键引用批次，应用内没有删除批次的入口，因此冻结条目始终可下载。

就绪判定（`service.manifest_readiness`）：

| 情况 | 分类 |
| --- | --- |
| 已发布 run 绑定当前底图，且无排队/运行中的计算 | 就绪 |
| 有排队/运行中的计算，或已发布结果基于旧底图且尚未重算 | 待算 |
| 最近一次计算失败且没有基于当前底图的已发布结果（含按新底图重算失败） | 失败 |
| 尚未发布过任何归属结果 | 缺失 |
| 尚未发布底图 | 全部批次均缺失 |

已有当前底图结果、但随后一次重算失败的批次仍算就绪（已发布结果不受失败影响），页面的
"说明"里会提示该次失败；这样数据员不必因为一次无关的失败重算而无法冻结已确认的结果。

---

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FOREST_DB_PATH` | `/data/forest.db` | SQLite 文件路径 |
| `FOREST_RUN_WORKER` | `1` | 是否在本实例内启动计算 worker |
| `FOREST_STALE_RUN_SECONDS` | `120` | 心跳超时后重新排队，需大于单批最长计算时间 |
| `FOREST_UPLOAD_MAX_MB` | `32` | 单个上传文件上限 |
| `FOREST_PREVIEW_ROWS` | `200` | 页面明细预览行数上限 |

---

## 目录结构

```
app/
  main.py          FastAPI 路由（上传/详情/下载/状态 JSON/供数清单/健康检查）
  service.py       版本发布、队列、计算、受保护发布、供数清单冻结、CSV 导出
  attribution.py   点面归属规则（唯一落区 / 边界 / 重叠 / 共边 / 区外）
  parsing.py       CSV 与 GeoJSON 校验、时刻规范化、几何修复
  db.py            SQLite schema、WAL、BEGIN IMMEDIATE 事务封装
  worker.py        后台计算线程（可崩溃恢复、可多实例并存）
  templates/       页面（含 manifests.html / manifest.html）
  static/style.css
samples/           含共边/重叠热点的演示数据
tests/             单元、API、供数清单、跨进程并发测试
```

---

## 已知边界

- SQLite 适合"单机/单卷 + 少量实例"的部署；若实例数或写入吞吐继续增长，`service.py` 的
  发布原语可迁移到 PostgreSQL（`SELECT ... FOR UPDATE` 替换写事务锁）。对业务语义无影响。
- 面积/距离按经纬度平面计算（GEOS 平面谓词）。归属判定（点是否在面内/面上）不受影响；
  若后续要做缓冲区分析，需要投影到米制坐标系。
- 上传与计算在 worker 线程中完成，单批超大（数十万行）时页面需等待计算完成才显示新结果；
  上一份已发布结果在此期间始终可下载。
