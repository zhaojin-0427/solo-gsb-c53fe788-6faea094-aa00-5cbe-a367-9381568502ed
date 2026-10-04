# 无人机航迹冲突消解台（Drone Conflict Console）

一个**本地运行**的 Web 应用：导入以 **米制三维坐标** 与 **相对起飞秒数** 表示的
分段直线航迹，系统在连续时间上检测冲突，并用 OR-Tools 求出让所有架次
**仅通过整体平移起飞时刻**即可无冲突的整数秒方案；无解时给出包含关系极小的
无解架次集合与互斥时差。支持锁定部分起飞时刻重算、页面对照“最早起飞”与
“求解后”两套航迹，方案可保存并随时回看（保留输入快照）。

---

## 功能一览

- **导入 JSON**：每个架次含固定三维分段直线航迹、整数秒起飞窗口 `[0, window_hi]`、
  正整数优先级权重 `weight`；只允许整体平移起飞时刻。
- **连续冲突检测**：在飞行时间重叠区间内逐时刻检查（含所有航段端点），
  当且仅当
  - 水平距离 `< 安全半径 R`，**且**
  - 垂直距离 `< 安全高度 H`

  时判定为冲突。水平/垂直是“与”的关系（相当于竖直安全圆柱）。
- **精确整数冲突模型**：对每对架次、每对航段，用解析几何
  （垂直条带裁剪 + 水平椭圆柱相交）求出会造成冲突的**相对起飞时差整数集合**，
  得到每对架次的“互斥时差区间”。
- **CP-SAT 求解（OR-Tools）**：选择每个架次的整数秒起飞偏移 `x_i ∈ [0, window_hi]`，
  避开所有互斥时差。
  - 第一目标：**加权总延误** `Σ w_i · x_i`（相对于各自最早起飞时刻）最小；
  - 第二目标：在最优目标值相同的方案中，取**按架次 ID 排序的起飞时刻序列字典序最小**，
    结果唯一、可复现。
- **无解诊断**：无解时列出若干**包含关系极小**的无解架次集合（删去其中任意一架次，
  其余架次即可消解），并给出每对架次的**互斥时差**（后者起飞 − 前者起飞，秒）。
- **锁定重算**：在页面勾选锁定并填写某些架次的整数秒偏移，重新求解其余架次。
- **执行批次与偏差预警**：从已保存的可行方案创建**执行批次**，冻结输入航迹快照、
  安全阈值与计划偏移；之后对原场景如何重算都不会改变批次。各架次以**非负整数秒
  实际起飞偏移 + 修订号**（首版 1、严格递增）上报；同号同值重试幂等，同号异值或
  旧号返回冲突。上报裁决在存储层用**数据库级条件写入**完成，**多个进程共用同一
  SQLite 文件并发上报时也不会发生修订回退**：高修订号先提交则低号得到带
  `current_revision` 的旧号冲突，低号先提交则高号仍可更新，终态恒为最高修订号
  （并发首报同号也按幂等/冲突处理）。给定非负当前秒 `t` 做预警：早于任一已
  上报实际起飞时刻则拒绝；已上报按实际起飞、未上报且计划起飞不早于 `t` 按计划
  起飞、其余标为**逾期**并排除预测（结果标明“预测不完整”）；沿用连续时间冲突
  判定返回 `t` 及之后的冲突区间、首个冲突时刻与已上报架次的偏离计划秒数。
- **预警变更复盘**：每次**被接受**的上报在**同一数据库事务**内落一个复盘节点
  （按架次严格递增的提交序号 + 该次写入前后完整上报状态快照），因此上报写入与
  节点天然原子对应，多进程共库时节点顺序即实际提交顺序；同号同值重试、同号异值、
  旧号等未被接受的写入**不产生节点**。任选一个已接受的上报节点与非负当前秒 `t`
  即可复盘：从 SQLite 还原该节点前后完整上报状态，用**同一个 `t`** 分别评估连续
  时间预警，按架次对列出**新增**与**解除**的冲突时间段（两区间相交时只标变化的
  部分），并给出前后首个冲突时刻、逾期架次变化与预测完整性变化，同时标明触发
  本次变更的架次与修订号。`t` 早于任一侧已上报的实际起飞时刻时按现有规则**整次
  拒绝**（不返回差异）；未知节点返回 `404`。升级前已存在的批次不补造历史节点：
  其首个升级后节点的“前状态”即升级时可知的最新状态。
- **可视化对照**：
  - 水平航迹图（最早起飞虚线 / 求解后实线，冲突见证点用红色 ✕ 与安全圆标出）；
  - 甘特式时间轴（红色段为最早起飞时的冲突时间）；
  - 高度剖面图；
  - 时间游标可拖动，观察各机实时位置。
- **方案留存**：保存方案时连同**输入 JSON 快照**、安全参数与求解结果一并存入
  本地 SQLite，之后可随时打开回看或删除。

---

## 目录结构

```
drone-conflict-console/
├── app/
│   ├── main.py          # FastAPI 路由（含执行批次 / 上报 / 预警 API）
│   ├── model.py         # 航迹/航段数据模型
│   ├── geometry.py      # 连续冲突检测 + 互斥时差解析几何
│   ├── solver.py        # OR-Tools CP-SAT 建模、字典序择优、无解核枚举
│   ├── batch.py         # 批次预警纯逻辑：状态分类 + 连续时间冲突投影 + 复盘差异
│   ├── schemas.py       # 导入/求解/上报请求的校验
│   ├── storage.py       # SQLite 持久化（场景、方案、批次、上报、复盘节点）
│   └── static/          # 原生 JS + Canvas 前端（无构建步骤）
├── examples/crossroads.json
├── scripts/
│   └── verify_multiprocess.py  # 双进程共库并发上报与复盘节点的 HTTP 级验证脚本
├── tests/               # 几何交叉验证 + 求解器暴力验证 + 批次/预警语义 + 复盘
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

---

## 一键启动（Docker Compose，推荐）

需要安装 Docker 与 Docker Compose（v2）。

```bash
docker compose up --build
```

启动后用浏览器访问：

- **应用页面**：<http://localhost:8000/>
- API 健康检查：<http://localhost:8000/api/health>
- OpenAPI 文档（可选）：<http://localhost:8000/docs>

数据保存在 Docker 命名卷 `dcc-data`（容器内 `/data/console.db`），
停止/重建容器不会丢失已保存方案。

停止：

```bash
docker compose down          # 保留数据
docker compose down -v       # 同时删除已保存数据
```

### 双进程共用同一数据库（验证并发上报）

默认 `docker compose up` 只启动一个服务进程。若要验证**两个进程同时读写同一份
SQLite**（例如多副本部署或多 worker）时的修订裁决，可加 `multiproc` profile
额外启动第二个进程 `web2`：它挂载同一个 `dcc-data` 卷、使用同一个
`DCC_DB_PATH=/data/console.db`，对外暴露在 **8001** 端口：

```bash
docker compose --profile multiproc up --build
# 进程 1 -> http://localhost:8000   （容器内 /data/console.db）
# 进程 2 -> http://localhost:8001   （同一个文件）
```

然后在宿主机（装有 Python 3 即可，仅用标准库）运行验证脚本，它会分别向
8000 / 8001 两个进程并发提交上报，并交叉核对批次详情与预警：

```bash
python3 scripts/verify_multiprocess.py
# 自定义地址： BASE1=http://... BASE2=http://... python3 scripts/verify_multiprocess.py
```

脚本覆盖：修订 3 先提交时，另一进程提交修订 2 返回 `409 stale_revision` 且
`current_revision=3`；修订 2 先提交时，修订 3 仍可更新；两个进程**同时**提交
2 与 3（多轮，含两种提交先后）后，批次详情、页面数据与预警一律使用最高修订 3
的实际偏移；以及并发首报按同号规则处理（同值一插一幂等）。每个用例还会核对
**复盘节点**：被接受的写入按实际提交顺序生成节点（修订 2 先提交则节点为
`[1,2,3]`，修订 3 先提交则修订 2 被拒、节点为 `[1,3]`），前后状态快照逐节点
衔接，且从两个进程分别复盘最新节点结果完全一致。
结束后 `docker compose --profile multiproc down` 停止两个进程。

---

## 本地直接运行（不使用 Docker）

需要 Python 3.11+。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --host 127.0.0.1 --port 8000
# 或： python -m uvicorn app.main:app --port 8000
```

浏览器打开 <http://127.0.0.1:8000/>。
数据库默认写在 `./data/console.db`，可用环境变量 `DCC_DB_PATH` 改路径。

---

## 使用方式

1. 打开页面，点右上角 **导入航迹 JSON**（或点 **载入示例** 体验
   `examples/crossroads.json`）。
2. 在左侧调整 **安全半径 / 安全高度**（修改后会自动重新求解）。
3. 右侧查看：
   - 水平航迹、时间轴、高度剖面；拖动顶部时间游标看实时位置；
   - 底部结果面板显示是否有解、加权总延误、各架次起飞偏移；
   - 无解时面板列出极小无解架次集合与互斥时差。
4. 如需固定某些架次：在 **架次与锁定** 中勾选“锁定”，填整数秒偏移，
   再点顶部 **重算（应用锁定）**。
5. 点 **保存当前方案** 命名存档；之后在左下 **已保存方案** 中点击即可
   连同输入快照重新载入，🗑 可删除。
6. 得到可行方案后，在 **执行批次与偏差预警** 卡片点 **从当前方案创建执行批次**
   （输入航迹、安全阈值、计划偏移随即冻结）。在批次详情中：
   - 每个架次一行，填写**非负整数秒实际起飞偏移**后点 **上报**，修订号自动取
     “当前 +1”（首版为 1）；同号同值重发安全（幂等），同号异值 / 旧号会弹冲突提示。
   - 填写 **当前秒 t**，点 **评估预警**：状态徽标显示“已上报 / 按计划 / 逾期”，
     面板给出预测是否完整、首个冲突时刻、各已上报架次偏离计划秒数，以及 `t` 及
     之后的连续时间冲突区间。页面与 HTTP API 读写同一批次数据。
   - 下方 **预警变更复盘** 中选择一个已接受的上报节点（下拉框按提交顺序列出，
     默认最新），点 **复盘该节点**：用上方同一个 `t` 对比该节点前后预警，显示
     触发架次与修订号、前后首个冲突时刻、逾期变化，以及按架次对列出的新增 /
     解除冲突时间段（相交区间只标变化部分）。

---

## 输入 JSON 格式

顶层为一个对象（也可以先用 `examples/crossroads.json` 对照）：

```json
{
  "safety_radius": 30,
  "safety_height": 10,
  "sorties": [
    {
      "id": "UAV-A",
      "weight": 3,
      "window_hi": 20,
      "waypoints": [
        {"x": 0,   "y": 0,  "z": 20, "t": 0},
        {"x": 120, "y": 0,  "z": 20, "t": 12},
        {"x": 240, "y": 0,  "z": 20, "t": 24}
      ]
    }
  ]
}
```

| 字段 | 含义 | 约束 |
| --- | --- | --- |
| `safety_radius` | 水平安全半径（米） | 正数 |
| `safety_height` | 垂直安全高度（米） | 正数 |
| `sorties[].id` | 架次唯一标识 | 非空、不重复 |
| `sorties[].weight` | 优先级权重 | **正整数**，越大延误代价越高 |
| `sorties[].window_hi` | 最晚相对起飞时刻（秒） | 非负整数；起飞窗口为 `[0, window_hi]` |
| `waypoints[].x/y/z` | 米制三维坐标 | 数值 |
| `waypoints[].t` | 相对本架次起飞的航点时刻（秒） | **非负整数，严格递增**，至少 2 个航点 |

说明：相邻航点之间以匀速直线运动相连；导入时每个架次的最早起飞时刻统一记为 0，
求解得到的 `offset` 即“相对最早起飞的整数秒平移量”，最终实际起飞时刻 = 该偏移。

导入接口：`POST /api/import`（页面导入时安全参数以 JSON 内的为准；
后续可在 `/api/scenarios/{id}/solve` 请求体中临时覆盖）。

---

## 求解与无解说说明（建模要点）

- **互斥时差**：对架次 A、B，若 `x_B − x_A` 落入某整数区间 `[lo, hi]` 就会冲突，
  则求解器施加析取约束 `x_B − x_A ≥ hi+1 ∨ x_B − x_A ≤ lo−1`。
- 这些区间来自**连续时间解析几何**（而非秒级采样），保证不漏检；
  代码中的检测对端点与擦边情形按安全侧（落在边界也判冲突）处理。
- **无解核（MUS）**：每个架次带一个“参与”布尔并作为 CP-SAT 假设；
  返回的不可满足核心天然是包含关系极小的。再通过“强制某架次退出”分支，
  枚举出更多极小集合，并附带每对架次的互斥时差。
- 目标采用两阶段优化：先最小化加权总延误并固定最优值，
  再按架次 ID 逐个最小化偏移，得到唯一的字典序最优时刻序列。

---

## API 摘要

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `POST` | `/api/import` | 导入航迹 JSON，返回架次/互斥时差/最早起飞冲突 |
| `POST` | `/api/scenarios/{id}/solve` | 按（可覆盖的）安全参数与锁定重算 |
| `POST` | `/api/scenarios/{id}/save` | 命名保存当前方案（含输入快照） |
| `GET` | `/api/scenarios` | 已保存方案列表 |
| `GET` | `/api/scenarios/{id}` | 回看某个方案（输入快照 + 求解结果） |
| `DELETE` | `/api/scenarios/{id}` | 删除 |
| `POST` | `/api/scenarios/{id}/batches` | 从可行方案创建执行批次（冻结快照/阈值/计划） |
| `GET` | `/api/batches?scenario_id=` | 批次列表（可按场景过滤） |
| `GET` | `/api/batches/{id}` | 批次详情（冻结计划 + 全部上报 + 复盘节点列表） |
| `DELETE` | `/api/batches/{id}` | 删除批次及其上报 |
| `PUT` | `/api/batches/{id}/reports` | 上报某架次实际起飞偏移与修订号（幂等/冲突，多进程共库也按数据库级规则裁决） |
| `GET` | `/api/batches/{id}/warning?t=` | 在非负当前秒 `t` 评估偏差预警 |
| `GET` | `/api/batches/{id}/replay/{node}?t=` | 复盘第 `node` 个已接受上报节点：同一 `t` 下前后预警差异（未知节点 404） |
| `GET` | `/api/health` | 健康检查 |

命令行示例：

```bash
curl -X POST http://localhost:8000/api/import \
  -H 'Content-Type: application/json' \
  --data @examples/crossroads.json
```

---

## 执行批次 / 偏差预警：可复现演练

下面所有请求/响应均由本仓库 `examples/crossroads.json` 在 `t` 评估时实跑得到
（`created_at`、`reported_at` 为时间戳，每次运行会不同；冲突数值由连续时间几何
确定，稳定可复现）。先 `docker compose up` 启动服务。

### 1) 导入 → 求解 → 保存 → 建批次

```bash
# 导入
curl -s -X POST http://localhost:8000/api/import \
  -H 'Content-Type: application/json' \
  --data @examples/crossroads.json
# => {"scenario_id":1, ...}

# 求解（得到唯一字典序最优计划偏移）
curl -s -X POST http://localhost:8000/api/scenarios/1/solve \
  -H 'Content-Type: application/json' -d '{"time_limit":20}'
# => solution: {"feasible":true,"status":"optimal","weighted_delay":28,
#              "offsets":{"UAV-A":4,"UAV-B":0,"UAV-C":2,"UAV-D":6}}

# 保存
curl -s -X POST http://localhost:8000/api/scenarios/1/save \
  -H 'Content-Type: application/json' -d '{"name":"crossroads-plan"}'
# => {"scenario_id":1,"name":"crossroads-plan","saved":true}

# 从可行方案创建执行批次（冻结输入航迹 / 安全阈值 / 计划偏移）
curl -s -X POST http://localhost:8000/api/scenarios/1/batches \
  -H 'Content-Type: application/json' -d '{"name":"演练批次-1"}'
```

响应：

```json
{"batch_id":1,"scenario_id":1,"name":"演练批次-1",
 "safety_radius":30.0,"safety_height":10.0,
 "planned_offsets":{"UAV-A":4,"UAV-B":0,"UAV-C":2,"UAV-D":6},"reports":[]}
```

对场景 1 再做带锁定的重算（`locks=[{"sortie_id":"UAV-B","offset":5}]`）会得到新的
计划偏移 `{"UAV-A":4,"UAV-B":5,"UAV-C":10,"UAV-D":11}`，但批次 1 的
`planned_offsets` 仍是 `{4,0,2,6}`——**原方案重算不改变批次**。

### 2) 上报实际起飞（修订协议）

```bash
# 首版修订号必须为 1：UAV-A 实际晚 3 秒起飞（实际偏移 7）
curl -s -X PUT http://localhost:8000/api/batches/1/reports \
  -H 'Content-Type: application/json' \
  -d '{"sortie_id":"UAV-A","actual_offset":7,"revision":1}'
# => {"batch_id":1,"report":{"sortie_id":"UAV-A","actual_offset":7,"revision":1},
#     "accepted":true,"idempotent":false}

# 同号同值重试：幂等
curl -s -X PUT http://localhost:8000/api/batches/1/reports \
  -H 'Content-Type: application/json' \
  -d '{"sortie_id":"UAV-A","actual_offset":7,"revision":1}'
# => ... "accepted":true,"idempotent":true

# 同号异值：409 冲突
curl -i -X PUT http://localhost:8000/api/batches/1/reports \
  -H 'Content-Type: application/json' \
  -d '{"sortie_id":"UAV-A","actual_offset":8,"revision":1}'
# HTTP/1.1 409 Conflict
# {"detail":{"reason":"revision_value_conflict","sortie_id":"UAV-A","revision":1,
#            "stored_actual_offset":7,"received_actual_offset":8}}
```

纠错（更高修订号）与旧号冲突：

```bash
# 修订号 2 纠正为实际偏移 4（回到计划）
curl -s -X PUT http://localhost:8000/api/batches/1/reports \
  -H 'Content-Type: application/json' \
  -d '{"sortie_id":"UAV-A","actual_offset":4,"revision":2}'
# => ... "report":{"sortie_id":"UAV-A","actual_offset":4,"revision":2}

# 再发旧修订号：409
curl -i -X PUT http://localhost:8000/api/batches/1/reports \
  -H 'Content-Type: application/json' \
  -d '{"sortie_id":"UAV-A","actual_offset":4,"revision":1}'
# HTTP/1.1 409 Conflict
# {"detail":{"reason":"stale_revision","sortie_id":"UAV-A",
#            "received_revision":1,"current_revision":2}}
```

约束：`actual_offset`、`revision` 为整数，`actual_offset >= 0`、`revision >= 1`，
否则 `422`；未知批次/架次返回 `404`。

#### 并发与多进程共库的修订裁决

裁决由**存储层在数据库内**完成，而不是“先读当前修订号、再无条件写回”，因此在
**多个进程共享同一个 SQLite 文件**时也正确（进程内的线程锁无法跨进程互斥，
而且 SQLite 的 WAL 快照会让两个并发事务都读到旧值）。`submit_report` 在单个
`BEGIN IMMEDIATE` 事务里只执行**带条件的写**：

- 首报：`INSERT ... SELECT ... WHERE revision = 1 AND NOT EXISTS(该架次的行)`，
  无行且修订号为 1 才插入；
- 更高修订：`UPDATE ... WHERE 入库修订号 < 本次修订号`，仅当现有修订号**严格
  更小**才覆盖；同号写入不会改变既有值。

写条件针对的是“写时刻已提交的行”，所以无论两个重叠事务以何种顺序提交，低修订号
都不可能覆盖高修订号。写后在同一事务内重新读取权威行来判定结果，落败的一方据此
拿到赢家的当前修订号。于是：

- 修订 3 先提交，另一进程提交修订 2 → `409 {"reason":"stale_revision",
  "current_revision":3}`；
- 修订 2 先提交（覆盖 1），修订 3 随后仍可更新；
- 两个进程**同时**提交 2 与 3 → 终态恒为修订 3 的实际偏移；
- 并发首报（都为修订 1）按同号规则处理：同值一“插入”一“幂等”，异值一方
  `revision_value_conflict`。

两次请求结束后，`GET /api/batches/{id}` 批次详情、页面（`/app`）与
`GET .../warning` 预警都只读取这一条最高修订对应的实际偏移。数据库以 WAL 打开，
并设置了 `busy_timeout`，第二个进程与首个进程同时初始化时会在锁上重试。

### 3) 评估预警（含拒绝、逾期、不完整标记）

当前秒 `t` 早于任一已上报实际起飞时刻则拒绝（`t` 本身要求非负整数，否则 `400/422`）：

```bash
curl -i "http://localhost:8000/api/batches/1/warning?t=0"
# HTTP/1.1 400 Bad Request
# {"detail":{"reason":"t_before_reported_takeoff","t":0,
#            "violations":[{"sortie_id":"UAV-A","actual_takeoff":4}]}}
```

未上报且计划起飞已严格早于 `t` 的架次被判**逾期**、排除预测，并标记预测不完整
（全新批次，计划为 A=4/B=0/C=2/D=6，`t=3`）：

```bash
curl -s "http://localhost:8000/api/batches/3/warning?t=3"
```

```json
{"t":3,"complete":false,"incomplete":true,
 "overdue_sorties":["UAV-B","UAV-C"],
 "sorties":[
   {"id":"UAV-A","status":"planned","planned_offset":4,"actual_offset":null,
    "revision":null,"takeoff_offset":4,"deviation_seconds":null},
   {"id":"UAV-B","status":"overdue","planned_offset":0,"actual_offset":null,
    "revision":null,"takeoff_offset":null,"deviation_seconds":null},
   {"id":"UAV-C","status":"overdue", "...": "..."},
   {"id":"UAV-D","status":"planned","planned_offset":6,"takeoff_offset":6, "...": "..."}],
 "conflicts":[],"first_conflict_time":null,"reported_deviations":[]}
```

### 4) 偏差引发的连续时间冲突预警

新建批次并上报：A 实际 4（计划 4）、**B 实际 4（计划 0，偏离 +4 秒）**、
C 实际 2（计划 2）；D 未上报。在 `t=4`（D 的计划起飞 6 不早于 4，故按计划预测）：

```bash
curl -s -X POST http://localhost:8000/api/scenarios/1/batches \
  -H 'Content-Type: application/json' -d '{"name":"演练批次-2（含偏差）"}'
# 批次 id=2
curl -s -X PUT http://localhost:8000/api/batches/2/reports -H 'Content-Type: application/json' \
  -d '{"sortie_id":"UAV-A","actual_offset":4,"revision":1}'
curl -s -X PUT http://localhost:8000/api/batches/2/reports -H 'Content-Type: application/json' \
  -d '{"sortie_id":"UAV-B","actual_offset":4,"revision":1}'
curl -s -X PUT http://localhost:8000/api/batches/2/reports -H 'Content-Type: application/json' \
  -d '{"sortie_id":"UAV-C","actual_offset":2,"revision":1}'
curl -s "http://localhost:8000/api/batches/2/warning?t=4"
```

响应（节选）：

```json
{"t":4,"complete":true,"incomplete":false,"overdue_sorties":[],
 "first_conflict_time":9.942324,
 "sorties":[
   {"id":"UAV-A","status":"reported","planned_offset":4,"actual_offset":4,
    "revision":1,"takeoff_offset":4,"deviation_seconds":0},
   {"id":"UAV-B","status":"reported","planned_offset":0,"actual_offset":4,
    "revision":1,"takeoff_offset":4,"deviation_seconds":4},
   {"id":"UAV-C","status":"reported","planned_offset":2,"actual_offset":2,
    "revision":1,"takeoff_offset":2,"deviation_seconds":0},
   {"id":"UAV-D","status":"planned","planned_offset":6,"actual_offset":null,
    "revision":null,"takeoff_offset":6,"deviation_seconds":null}],
 "conflicts":[
   {"sortie_a":"UAV-B","sortie_b":"UAV-C",
    "time_start":9.942324,"time_end":12.519215,"witness_time":11.230769,
    "a_pos":[120.0,12.307692,22.0],"b_pos":[110.769231,6.153846,24.0],
    "horizontal_distance":11.094004,"vertical_distance":2.0},
   {"sortie_a":"UAV-B","sortie_b":"UAV-D",
    "time_start":12.335899,"time_end":15.664101,"witness_time":14.0,
    "a_pos":[120.0,40.0,22.0],"b_pos":[120.0,40.0,18.0],
    "horizontal_distance":0.0,"vertical_distance":4.0}],
 "reported_deviations":[
   {"sortie_id":"UAV-A","planned_offset":4,"actual_offset":4,"revision":1,"deviation_seconds":0},
   {"sortie_id":"UAV-B","planned_offset":0,"actual_offset":4,"revision":1,"deviation_seconds":4},
   {"sortie_id":"UAV-C","planned_offset":2,"actual_offset":2,"revision":1,"deviation_seconds":0}]}
```

冲突只取 `t=4` 及之后的连续时间区间；当 `t` 落在某区间内部时，`time_start` 会被
裁剪到 `t`、`witness_time` 与见证位置相应重算，`first_conflict_time` 同步为裁剪后
的起点；区间整体早于 `t` 时不再返回。**纠错后重算**（对 B 再发更高修订号）或
**批次重载**（`GET /api/batches/2` 后重新 `warning`）结果完全一致——预警是冻结
批次与已存上报的纯函数。

### 5) 预警变更复盘（按上报节点对比前后预警）

每次**被接受**的上报都会留下一个复盘节点；批次详情的 `events` 字段按提交顺序
列出（接第 4 节的批次 2，其三次上报即节点 #1~#3）：

```bash
# 对 B 再发更高修订号纠错：实际偏移 4 -> 2（生成节点 #4）
curl -s -X PUT http://localhost:8000/api/batches/2/reports -H 'Content-Type: application/json' \
  -d '{"sortie_id":"UAV-B","actual_offset":2,"revision":2}'

curl -s http://localhost:8000/api/batches/2 | python3 -m json.tool
```

```json
"events":[
  {"seq":1,"sortie_id":"UAV-A","actual_offset":4,"revision":1,"created_at":...},
  {"seq":2,"sortie_id":"UAV-B","actual_offset":4,"revision":1,"created_at":...},
  {"seq":3,"sortie_id":"UAV-C","actual_offset":2,"revision":1,"created_at":...},
  {"seq":4,"sortie_id":"UAV-B","actual_offset":2,"revision":2,"created_at":...}]
```

复盘节点 #4（B 由 4 纠正为 2）：前后完整上报状态从 SQLite 还原，两侧用**同一个
`t=4`** 评估连续时间预警；`node` 标明触发本次变更的架次与修订号：

```bash
curl -s "http://localhost:8000/api/batches/2/replay/4?t=4" | python3 -m json.tool
```

```json
{"batch_id":2,
 "node":{"seq":4,"sortie_id":"UAV-B","actual_offset":2,"revision":2,"created_at":...},
 "t":4,
 "reports_before":[{"sortie_id":"UAV-A","actual_offset":4,"revision":1},
                   {"sortie_id":"UAV-B","actual_offset":4,"revision":1},
                   {"sortie_id":"UAV-C","actual_offset":2,"revision":1}],
 "reports_after": [{"sortie_id":"UAV-A","actual_offset":4,"revision":1},
                   {"sortie_id":"UAV-B","actual_offset":2,"revision":2},
                   {"sortie_id":"UAV-C","actual_offset":2,"revision":1}],
 "before":{"t":4, "...": "完整预警（结构同 /warning）",
           "conflicts":[{"sortie_a":"UAV-B","sortie_b":"UAV-C","time_start":9.942324,"time_end":12.519215,"...":"..."},
                        {"sortie_a":"UAV-B","sortie_b":"UAV-D","time_start":12.335899,"time_end":15.664101,"...":"..."}]},
 "after": {"t":4, "...": "...",
           "conflicts":[{"sortie_a":"UAV-B","sortie_b":"UAV-C","time_start":9.528191,"time_end":11.394886,"...":"..."},
                        {"sortie_a":"UAV-B","sortie_b":"UAV-D","time_start":12.0,"time_end":14.0,"...":"..."}]},
 "diff":{
   "pairs":[
     {"sortie_a":"UAV-B","sortie_b":"UAV-C",
      "added":[[9.528191,9.942324]],"resolved":[[11.394886,12.519215]]},
     {"sortie_a":"UAV-B","sortie_b":"UAV-D",
      "added":[[12.0,12.335899]],"resolved":[[14.0,15.664101]]}],
   "first_conflict_time_before":9.942324,"first_conflict_time_after":9.528191,
   "complete_before":true,"complete_after":true,
   "overdue_before":[],"overdue_after":[],
   "overdue_added":[],"overdue_resolved":[]}}
```

注意 B↔C 的冲突区间由 `[9.942324, 12.519215]` 变为 `[9.528191, 11.394886]`：两段
区间相交，差异里**只标变化部分**——新增 `[9.528191, 9.942324]`、解除
`[11.394886, 12.519215]`，相交的公共部分不重复出现；B↔D 同理。

复盘节点 #2（B 首次上报偏离 +4 秒）在 `t=4`：冲突 B↔D 整段**新增**，同时 B 由
“逾期”变为“已上报”（逾期解除），预测完整性也随之变化：

```bash
curl -s "http://localhost:8000/api/batches/2/replay/2?t=4" | python3 -c \
  "import json,sys; print(json.dumps(json.load(sys.stdin)['diff'],indent=1))"
```

```json
{"pairs":[{"sortie_a":"UAV-B","sortie_b":"UAV-D",
           "added":[[12.335899,15.664101]],"resolved":[]}],
 "first_conflict_time_before":null,"first_conflict_time_after":12.335899,
 "complete_before":false,"complete_after":false,
 "overdue_before":["UAV-B","UAV-C"],"overdue_after":["UAV-C"],
 "overdue_added":[],"overdue_resolved":["UAV-B"]}
```

`t` 早于**任一侧**已上报的实际起飞时刻时，按与 `/warning` 相同的规则**整次
拒绝**（不返回差异，`side` 标明触发拒绝的一侧）；未知节点返回 `404`：

```bash
curl -i "http://localhost:8000/api/batches/2/replay/4?t=1"
# HTTP/1.1 400 Bad Request
# {"detail":{"reason":"t_before_reported_takeoff","t":1,"side":"before",
#            "violations":[{"sortie_id":"UAV-A","actual_takeoff":4},
#                          {"sortie_id":"UAV-B","actual_takeoff":4},
#                          {"sortie_id":"UAV-C","actual_takeoff":2}]}}
curl -i "http://localhost:8000/api/batches/2/replay/99?t=4"
# HTTP/1.1 404 Not Found
# {"detail":"report node not found"}
```

节点规则：同号同值重试（幂等）、同号异值、旧号、首版非 1 等**未被接受**的写入
都不产生节点；并发升版时节点顺序即实际提交顺序（修订 2 先提交则节点为
`[…, 2, 3]`，修订 3 先提交则修订 2 被拒、不留节点）；复盘是冻结批次与已存节点
的纯函数，重载后结果一致。升级前已存在的批次没有历史节点，其首个升级后节点
的“前状态”就是升级时可知的最新状态，更早的记录不补造。

---

## 测试

测试不依赖容器，在本地虚拟环境中运行：

```bash
pip install -r requirements.txt
python tests/test_geometry_crossval.py     # 几何：解析法 vs 连续扫描 vs 密采样
python tests/test_solver_bruteforce.py 2026  # 求解器：对照暴力枚举（含无解核极小性）
python tests/test_batches_warning.py       # 批次：修订协议/多进程共库并发/预警分类与冲突
python tests/test_replay.py                # 复盘：节点规则/提交顺序/区间差异/HTTP 语义
```

- `test_geometry_crossval.py` 用三套相互独立的实现（多边形/椭圆柱解析、
  连续时间区间剖分、密采样神谕）对上千个随机航段对与擦边案例做一致性校验；
- `test_solver_bruteforce.py` 在小窗口下穷举所有起飞组合，验证最优目标、
  字典序择优、锁定约束以及无解核的可行性与包含极小性；
- `test_batches_warning.py` 验证批次冻结、首版为 1/严格递增/幂等/冲突、
  **两个 OS 进程共享同一 SQLite 文件时并发上报仍只留最高修订号**（用
  `multiprocessing` 真起两个进程对同一文件做条件写入，含修订 2/3 竞争与
  并发首报），以及预警的拒绝、已上报/按计划/逾期分类、不完整标记、连续时间
  冲突区间向 `t` 投影与“重载结果一致”；
- `test_replay.py` 验证复盘节点与上报写入的原子对应（被接受才落节点、同号同值
  重试与被拒修订不留节点、前后状态快照逐节点衔接）、既有批次以升级时最新状态
  为基线且不补造历史、线程与**两个 OS 进程**并发升版时节点按实际提交顺序排列、
  区间集合差（相交只标变化部分、点区间、端点相接）、逾期变化与首个冲突时刻，
  以及 HTTP 层的未知节点 404、负 `t` 400、任一侧过早 `t` 整次拒绝、现有上报
  响应与最新预警保持兼容、重载复盘一致。

---

## 备注与边界

- 冲突为严格不等式（`水平 < R` 且 `垂直 < H`）；实现采用极小容差按安全侧处理，
  恰好落在边界的情形会被当作冲突，宁可保守。
- 起飞时刻与航点时刻均为整数秒；航迹在两段之间为连续匀速直线，
  冲突检测本身在连续时间上进行。
- 求解时限可在页面或请求中设置（默认 20 秒）。正常规模下加权延误优化通常
  很快即可得到 `optimal`；超时会返回当前最优 `feasible` 并在状态中标明。
- 执行批次只允许从**可行方案**创建；批次保存的是独立的输入/阈值/计划快照，
  场景的后续重算、改名或删除不影响已建批次的预警计算（删除场景会级联移除其
  批次，依赖 SQLite 外键 `ON DELETE`）。
- 预警的“计划起飞不早于 `t`”按 `计划偏移 >= t` 判定（恰在 `t` 起飞仍可预测）；
  逾期架次不参与任何冲突对，因此只要存在逾期架次 `incomplete=true`。已上报架次
  的偏离秒数 = 实际偏移 − 计划偏移（负为提前、正为延误）。
- 多个进程/副本可共用同一个 `DCC_DB_PATH` 指向的 SQLite 文件：数据库以 WAL 打开、
  设置了忙等待，上报用条件写入仲裁，并发修订不会回退（可用
  `docker compose --profile multiproc up` 起两个进程并运行
  `scripts/verify_multiprocess.py` 验证）。
- 复盘节点（`report_events` 表）与上报写入在**同一个 `BEGIN IMMEDIATE` 事务**内
  提交：写入前先在写锁保护下读取完整上报状态作为“前状态”快照，条件写入后再读
  出“后状态”快照，节点序号 `seq` 按 `MAX(seq)+1` 在持锁期间分配，因此跨进程共库
  时节点顺序就是实际提交顺序，且上报行与节点要么同时可见、要么都不可见；未被
  接受的写入（幂等重试、同号异值、旧号、首版非 1）不执行任何写，自然不留节点。
- 复盘接口 `GET /api/batches/{id}/replay/{node}?t=` 只读取冻结批次与已存节点：
  前后两侧用**同一个非负整数 `t`** 评估；任一侧出现“`t` 早于已上报实际起飞”
  即整次 `400` 拒绝（响应带 `side` 标明是哪一侧，不含差异）；节点不存在返回
  `404`；差异按架次对给出新增/解除的连续时间区间，相交区间只保留变化部分
  （端点相接不算相交），并附前后首个冲突时刻、逾期集合变化与预测完整性变化。
- 升级前已存在的批次没有复盘节点，也不会补造：其升级后首个被接受的上报所生成
  的节点，以当时（即升级时可知的）最新上报状态为“前状态”基线。
