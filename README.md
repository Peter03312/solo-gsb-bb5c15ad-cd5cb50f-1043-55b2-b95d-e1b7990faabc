# 食品换线放行服务（共用产线过敏原裁决）

面向**共用产线**的食品换线放行：按产线顺序登记生产批次、所用原料批号及其过敏原、
成品声明过敏原、批次开工前清洁核验确认清除的过敏原；服务计算每批**实际可能含有的过敏原**、
**未声明项**，给出**放行 / 扣留结论及原因**，并支持原料批号与清洁核验更正后的**顺序重算**
与**可查询的历史裁决版本**。

此外支持**出货核验单**：一张核验单关联多个已登记生产批次，逐批冻结创建时的裁决版本与结论、
**出货资格版本**；草稿可重新核验刷新快照，确认时原子校验“快照版本仍为当前版本、
全部 RELEASE 且当前无待检锁定原料批号”；已确认单据所含批次一旦因更正重算产生新裁决版本
（即使结论仍为 RELEASE）或**出货资格版本**发生变化，立即转**待复核**，
重新核验后才能再次确认；历次确认快照只追加保留，HOLD 批次与待检锁定批号始终不能确认。

原料批号出现**待检风险**时，可按批号**锁定/解除**并填写原因（可查询当前状态与历次变更）。
锁定期间，**直接使用**该批号的生产批次即使过敏原裁决为 RELEASE 也不得通过出货核验；
同线后续批次若未直接使用该批号，**不因残留传递而连带锁定**，过敏原裁决与历史版本不变。
每次批号状态实际变化都使直接使用该批号的批次**出货资格版本**递增；未知批号、空原因拒绝，
重复提交相同状态不产生新变更。

生产现场可将**误登记、实际未投产**的批次按批次号并填写**非空原因**作废。作废**保留**原登记、
历史裁决、出货资格与作废原因供查询，**批次号不可复用**；作废批次不再参与同线残留传递、
当前出货资格统计或新核验。其后**有效批次按原顺序重算并追加裁决版本**；涉及这些批次
（或作废批次本身）的**已确认核验单转待复核**，历次确认快照不改。含作废批次的已有单据
**可查询，但重新核验与确认均拒绝**；作废批次不得更正。重复作废幂等返回既有结果且不重算；
未知批次、空原因拒绝，失败不留下部分版本。

生产批次**误归产线**时，可凭**有效批次号与另一条已登记产线编号**更正归属（换线）：
该批移至目标产线**末尾**并获得目标线**新的顺序号**，**批次号不变**，原线原顺序号
**留空而不复用**（其余批次顺序号不重排，新登记顺延下一号）。**原线从移出位置起、
目标线从移入批次起**，按各自有效批次顺序**重算过敏原残留与裁决**；每个被重算批次
（含移入批本身）**追加一个历史裁决版本**（trigger=`MOVE_BATCH`，即使结论不变也追加），
**历史版本中的原产线与顺序号一律不改**。**直接用料未变，出货资格版本不因换线递增**；
含这些批次的**已确认核验单转待复核**，历次确认快照保持原样。未知批次或目标线、
已作废批次、目标线与原线相同均整体拒绝，失败不留下部分归属或裁决版本。

**出货前装运批次发生变化**时，工作人员可凭**所见单据修订号**为草稿或待复核核验单
提交一份**完整、有序的新批次清单**（`POST /api/verifications/{no}/revise`）。保存时
在**同一事务内逐批重取当前裁决与出货资格快照**，单据回到**草稿**并**推进修订号**；
允许借此**移除旧清单中的已作废批次**，新清单中的批次必须**全部有效**。历次确认及其
当时的逐批快照**始终保留**，即使旧批次已不在现行清单；**已确认单据不可直接修订**。
空清单、重复或未知批次、含已作废批次、**过期修订号**均**整体拒绝**，当前成员与
快照不变；并发的批次更正或批号锁定在写事务内串行化，逐批快照取自同一事务快照，
单据不会混入不同时间的快照。重新核验、确认等其他单据快照写入同样推进修订号。

仓库完成**装车交接**时，可凭**唯一交接号、已确认核验单号及读取时修订号**登记
**整单发运**（`POST /api/handovers`）。交接只能针对**仍为已确认（CONFIRMED）且
修订号匹配**的单据；单内每个批次须**有效**、当前**裁决版本**与**出货资格版本**
与**最近一次确认快照一致**，且结论分别为 **RELEASE / SHIPPABLE**。**任一批次已在
其他交接记录中发运时整单拒绝**（批次全局唯一占用）。一次事务内占用全单批次并
**冻结**交接时的核验单号、修订号、逐批确认快照与交接时间；交接**不改写核验单**
（状态与修订号保持原样），其后的更正、待复核或清单修订**均不改写交接记录**，
可随时查询交接详情与**每批的交接归属**。重复交接号、未知单据、状态或版本冲突、
任一不合格批次均**整体拒绝，不留下部分占用**。原有核验及更正调用约定沿用不变。

- 技术栈：Python 3.11 + FastAPI + SQLite（标准库 `sqlite3`，WAL 模式）
- 数据落盘：单个 SQLite 文件，Docker 卷持久化
- 一键启动：`docker compose up`（首次启动自动建表、写入过敏原字典与演示原料批号）

---

## 1. 业务规则

同一产线 `line_id` 上，批次按登记顺序获得递增序号 `seq`，对第 N 批：

```
带入  = 上一批（第 N-1 批）实际可能含有的过敏原
残留  = 带入 − 本次清洁核验“确认清除”的过敏原     # 只清除被确认覆盖的项目
本批  = 本批所用全部原料批号过敏原的并集
实际  = 残留 ∪ 本批
未声明 = 实际 − 成品声明过敏原
结论  = 未声明非空 → HOLD（扣留）；否则 RELEASE（放行）
```

要点：

- **清洁只清除被确认覆盖的过敏原**：清洁清单中未列出的残留过敏原继续留在产线。
- **上一批即使被扣留（HOLD），其实际过敏原仍继续向下传递**。
- 清洁发生在本批开工前，只能清除“上一批留下的”残留；本批原料再次引入的过敏原仍计入实际。
- **更正原料批号或某批清洁/声明/用料后**：从受影响的最早批次开始，按产线顺序重算该批及
  之后的全部批次（原料批号更正会覆盖所有引用它的产线）；每次重算追加一个**只增不改的历史
  裁决版本**，可随时查询。
- 更正接口为**全量替换**语义（传入的数组代表更正后的完整集合，不传的字段保持不变）。

### 开工前清洁更正预演（只读）

生产主管在**正式更正某批开工前清洁记录前**，可先用同一次当前数据快照预演对同线后续
放行与已确认出货核验单的影响（`POST /api/batches/{batch_no}/cleaning/preview`，请求体
`{"cleaned_allergens": [...]}`，与正式清洁更正入口相同的全量集合与校验）：

- **逐批推演**：从拟更正批（含）起，按同线有效批次顺序逐批复算
  带入/残留/本批/实际/未声明与 RELEASE/HOLD；已作废批次不参与推演、自动跳过
  （作废批退出残留链，其清洁视为未发生）并在 `skipped_voided_batches` 中列出。
- **当前与预演并列返回**：每批给出 `current`（当前版本与裁决，含带入/残留/本批来源）
  与 `preview`（拟更正落库后的裁决，预演版本号 = 当前版本 + 1，trigger=`CORRECT_BATCH`），
  以及 `changed` 与逐条 `change_reasons`（清洁集合变化、残留/实际/未声明项变化、
  RELEASE↔HOLD 翻转）。
- **结论相同仍属版本影响范围**：从拟更正批起的每个**后续有效批次**都在范围内
  （`in_version_scope=true`）——即使预演裁决与当前完全一致，正式更正仍会为其追加一个
  裁决版本；`change_reasons` 给出 `NO_ADJUDICATION_CONTENT_CHANGE` 说明。更正点之前的
  批次不在范围内、不返回。
- **已确认核验单影响**：`affected_verifications` 仅列出**当前 CONFIRMED** 且含范围内
  批次的核验单——它们将在正式更正后因裁决版本追加而自动转**待复核**（即使结论仍为
  RELEASE）；逐批给出当前/预演裁决版本与结论。草稿/待复核单据不列入（本就待重新核验）。
- **待检锁定不参与过敏原推演**：预演只推演过敏原链路；清洁更正不改变直接用料，
  `shipment_eligibility.would_advance_if_applied` 恒为 `false`，并回显当前出货资格现状
  （锁定只影响出货资格）。
- **严格只读**：预演在一次读取快照内离线计算，**不更改**清洁记录、裁决/出货资格版本、
  核验单状态或任何历史；可重复调用，预演版本预测不随调用次数漂移。
- **整体报错**：未知批次 404 `CORRECTION_TARGET_NOT_FOUND`、已作废批次 409 `BATCH_VOIDED`、
  过敏原代码非法 400 `INVALID_ALLERGEN`（与正式更正入口一致，全部失败不留任何改动）。
- 预演结果与随后调用 `PUT /api/batches/{batch_no}/cleaning` 正式落库的重算结果逐批一致；
  原有更正与核验行为保持不变。

### 原料批号待检锁定与出货资格

原料批号可因待检风险按批号锁定、复检后解除（均须填写原因）：

- **出货资格只看“直接使用”**：一批的出货资格仅由该批 `material_lots` 中当前处于锁定状态的
  批号决定；同线后续批次若未直接使用该批号，**不因过敏原残留传递而连带锁定**，
  其过敏原裁决与历史裁决版本保持不变。
- **两套版本相互独立**：批号锁定不改变过敏原裁决（`adjudication.version` 不变）；
  每批另维护只增的**出货资格版本**（`shipment_eligibility.version`，初始为 1）：
  - 新登记批次按登记当时的批号锁定状态确定资格（`SHIPPABLE` / `BLOCKED`）；
  - 批号状态**每次实际变化**（锁↔解）都使所有直接使用该批号的批次出货资格版本 +1
    （即使该批因另一批号仍处于 BLOCKED、资格结论未翻转）；
  - 更正用料导致直接使用的锁定批号集合变化时，资格实际变化才递增并联动核验单；
  - 重复提交相同状态**不产生新变更**（锁定历史与资格版本均不变，返回 `changed=false`）。
- 已确认核验单所含批次的出货资格版本变化后，核验单立即转**待复核**。

### 误登记批次作废

生产现场可将误登记、实际未投产的批次按批次号作废（必须填写非空原因）：

- **保留可查、号不可复用**：作废不删除数据——原登记（用料/声明/清洁）、历史裁决版本、
  出货资格历史与作废原因/时间全部保留；批次详情、列表、历史均标明 `status=VOIDED` /
  `voided=true`；作废批次号再次登记按 `DUPLICATE_BATCH` 拒绝（seq 也不复用，新批顺延下一号）。
- **退出同线残留链**：作废批次视为从未投产，其登记时的清洁不再发生；从作废点之后一批起，
  后续**有效批次按原顺序重算**并追加裁决版本（trigger=`VOID_BATCH`，即使结论不变也追加）。
  作废批次本身**不产生**新裁决版本，其裁决/资格快照冻结保留。
- **不参与当前统计与新核验**：作废批次不出现在 `/api/eligibilities` 当前出货资格统计；
  批号锁定状态变化不联动作废批次（不产生新资格版本）；含作废批次的新核验单整体拒绝。
- **联动已有核验单**：作废批次本身、以及被重算的后续批次，其所在的**已确认**核验单立即转
  **待复核**（草稿不转态）；**历次确认快照只追加保留、一律不改**。
- **含作废批次的已有单据可查询，但重新核验与确认均拒绝**（409
  `VERIFICATION_CONTAINS_VOIDED_BATCH`，`details` 列出作废批次及原因）；单据详情逐批标明
  作废标记，顶层给出 `contains_voided_batches` / `voided_batches`。
- **作废批次不得更正**：批次更正与清洁更正入口对作废批次返回 409 `BATCH_VOIDED`；
  原料批号更正的重算只覆盖有效批次，作废批次历史冻结。
- **幂等与原子性**：重复作废返回既有结果（`changed=false`、首次原因、不重算）；
  未知批次 404、空白原因 400；整笔操作单事务，失败回滚，**不留下部分作废标记或部分版本**。

### 生产批次误归产线更正（换线）

生产批次误归产线时，可凭**有效批次号 + 另一条已登记产线编号**更正归属
（`POST /api/batches/{batch_no}/reassign`，请求体 `{"target_line_id": "L2"}`）：

- **批次号不变、移至目标线末尾**：批次只改归属产线与顺序号，目标线取“当前最大 seq + 1”；
  用料 / 声明 / 清洁等登记内容一律不动。
- **原线顺序号留空而不复用**：原线其余批次顺序号**不重排**（移出位置形成空洞），
  其后新登记批次继续顺延下一号；目标线新顺序号正常顺延。
- **两线各自按有效批次顺序重算**：
  - 原线**从移出位置起**重算（被移批次不再参与原线残留链，其登记时的清洁也随之移出）；
  - 目标线**从移入批次起**重算（移入批按目标线上一有效批的残留重新裁决，其后批次顺延）；
  - 每个被重算批次（**含移入批本身**）追加一个历史裁决版本（trigger=`MOVE_BATCH`，
    即使结论不变也追加）；移出点 / 移入点**之前**的批次不重算。
- **历史版本中的原产线和顺序号不改**：历史裁决版本逐版记录当时的 `line_id` / `seq`，
  换线前的版本保持原线旧序号，新版本记录目标线新序号（批次详情历史接口可查）。
- **直接用料未变，出货资格版本不得因换线递增**：`shipment_eligibility.version` 不变、
  资格历史不追加（批号锁定状态仍随直接用料关系保留，例如换线前 BLOCKED 的批次换线后
  仍 BLOCKED、版本不变）；批号锁定历史同样不变。
- **联动核验单**：含任一被重算批次（含移入批）的**已确认**核验单立即转**待复核**，
  草稿/待复核单据不转态；**历次确认快照只追加保留、一律不改**。重新核验后逐批快照
  刷新为新归属下的当前裁决/资格版本，方可再次确认。
- **拒绝与原子性**：未知批次 404、未知（未登记）目标线 404、已作废批次 409、
  目标线与原线相同 409、空白目标线 400；整笔操作单事务，失败回滚，
  **不留下部分归属或部分裁决版本**。

换线相关拒绝码：

| 场景 | HTTP | error.code |
| --- | --- | --- |
| 更正归属的批次不存在 | 404 | `BATCH_NOT_FOUND` |
| 目标产线未登记 | 404 | `LINE_NOT_FOUND` |
| 更正已作废批次的归属 | 409 | `BATCH_VOIDED` |
| 目标线与原产线相同 | 409 | `SAME_SOURCE_TARGET_LINE` |
| `target_line_id` 为空/纯空白或缺字段 | 400/422 | `INVALID_PAYLOAD` / FastAPI 校验 |

拒绝规则（失败时整个请求回滚，**不留下部分改动**）：

| 场景 | HTTP | error.code |
| --- | --- | --- |
| 批次号重复 | 409 | `DUPLICATE_BATCH` |
| 原料批号重复登记 | 409 | `DUPLICATE_MATERIAL_LOT` |
| 批次引用了未登记的原料批号 | 400 | `UNKNOWN_MATERIAL_LOT` |
| 过敏原代码不在受控字典内 | 400 | `INVALID_ALLERGEN` |
| 字段为空/类型错误等负载问题 | 400/422 | `INVALID_PAYLOAD` / FastAPI 校验 |
| 锁定/解除未登记的批号，或查询其状态 | 404 | `MATERIAL_LOT_NOT_FOUND` |
| 锁定/解除原因为空或纯空白 | 400 | `INVALID_PAYLOAD` |
| 批次作废原因为空或纯空白 | 400 | `INVALID_PAYLOAD` |
| 作废不存在的批次 | 404 | `BATCH_NOT_FOUND` |
| 更正/清洁更正已作废批次 | 409 | `BATCH_VOIDED` |
| 新核验单含已作废批次 | 400 | `BATCH_VOIDED` |
| 含作废批次的单据重新核验/确认 | 409 | `VERIFICATION_CONTAINS_VOIDED_BATCH` |
| 更正目标批次/原料批号不存在 | 404 | `CORRECTION_TARGET_NOT_FOUND` |
| 查询不存在的批次/原料批号 | 404 | `BATCH_NOT_FOUND` / `MATERIAL_LOT_NOT_FOUND` |

过敏原受控字典采用欧盟 14 类法定过敏原代码（仅接受这些代码）：

`gluten`（含麸质谷物）、`crustaceans`（甲壳类）、`egg`（蛋）、`fish`（鱼）、
`peanut`（花生）、`soy`（大豆）、`milk`（乳）、`tree_nuts`（坚果）、`celery`（芹菜）、
`mustard`（芥末）、`sesame`（芝麻）、`sulphites`（亚硫酸盐）、`lupin`（羽扇豆）、
`molluscs`（软体动物）。完整清单可通过 `GET /api/allergens` 查询。

---

## 2. 一键启动（Docker Compose，推荐）

```bash
docker compose up --build
# 后台运行：docker compose up -d --build
# 停止并保留数据：docker compose down
# 停止并删除数据卷（彻底重置）：docker compose down -v
```

容器首次启动自动完成：建表 → 写入 14 项过敏原字典 → 写入 4 个演示原料批号与示例产线 `L1`。
数据保存在命名卷 `changeover-data`（容器内 `/data/changeover.db`）。

## 3. 本地直接运行（不使用 Docker）

```bash
pip install -r requirements.txt

# 可选：初始化空库时写入演示原料批号与产线 L1
export SEED_DEMO_DATA=1

python -m app
```

本地默认数据库文件为仓库内 `data/changeover.db`（自动创建目录）。

## 4. 运行测试与示例

```bash
pip install pytest httpx
python -m pytest -q            # 164 个用例：裁决、传递、更正重算、历史、原子性、产线隔离、批号锁定、出货资格、出货核验单、批次作废、误归产线更正换线、清洁更正预演（只读）、出货前批次清单修订（乐观修订号/同事务快照/并发一致性）、装车交接（整单发运/冻结快照/唯一占用/交接归属/并发一致/旧库升级）、旧库升级

# 服务启动后，运行端到端调用示例（标准库 urllib，无需额外依赖）
python examples/run_demo.py
BASE_URL=http://127.0.0.1:8080 python examples/run_demo.py   # 显式指定地址

# 开工前清洁更正预演端到端示例（逐批当前/预演结论与变化原因、已确认核验单转待复核、只读性核对、拒绝场景、与正式更正一致）
python examples/cleaning_preview_demo.py
BASE_URL=http://127.0.0.1:8080 python examples/cleaning_preview_demo.py

# 出货核验单端到端示例（创建/拒绝/HOLD 拦截/确认/更正转待复核/重新核验/二次确认）
python examples/verification_demo.py
BASE_URL=http://127.0.0.1:8080 python examples/verification_demo.py

# 原料批号待检锁定端到端示例（锁定/解除/幂等/出货资格版本/残留不连带/核验单联动）
python examples/lot_lock_demo.py
BASE_URL=http://127.0.0.1:8080 python examples/lot_lock_demo.py

# 误登记批次作废端到端示例（作废保留/号不可复用/退出残留链重算/核验单联动/拒绝/幂等/查询标记）
python examples/batch_void_demo.py
BASE_URL=http://127.0.0.1:8080 python examples/batch_void_demo.py

# 误归产线更正（换线）端到端示例（两线重算/原线留空/历史产线序号不改/资格版本不递增/核验单联动）
python examples/batch_reassign_demo.py
BASE_URL=http://127.0.0.1:8080 python examples/batch_reassign_demo.py

# 出货前批次清单修订端到端示例（修订号/同事务快照/过期拒绝/移除作废批次/历史确认保留）
python examples/verification_revise_demo.py
BASE_URL=http://127.0.0.1:8080 python examples/verification_revise_demo.py

# 仓库装车交接端到端示例（整单发运/冻结快照/唯一占用/交接归属/各类整体拒绝）
python examples/handover_demo.py
BASE_URL=http://127.0.0.1:8080 python examples/handover_demo.py
```

---

## 5. 配置（环境变量）

| 变量 | 默认值（本地） | 容器默认 | 说明 |
| --- | --- | --- | --- |
| `HOST` | `0.0.0.0` | `0.0.0.0` | 监听地址 |
| `PORT` | `8080` | `8080` | 监听端口 |
| `DB_PATH` | `./data/changeover.db` | `/data/changeover.db` | SQLite 数据库文件路径 |
| `SEED_DEMO_DATA` | `0`（关） | `1`（开） | 仅在**全新空库**初始化时写入演示原料批号/产线；对已有库幂等、不重复写入 |

## 6. 服务地址与交互式文档

启动后默认地址端口：**http://127.0.0.1:8080**

- 健康检查：`GET /health`
- Swagger 交互文档：http://127.0.0.1:8080/docs
- OpenAPI 描述：http://127.0.0.1:8080/openapi.json

---

## 7. HTTP API 一览

### 参考数据

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/allergens` | 过敏原受控字典 |
| POST | `/api/material-lots` | 登记原料批号（lot_no、material、allergens） |
| GET | `/api/material-lots` | 列出全部原料批号（含当前锁定状态） |
| GET | `/api/material-lots/{lot_no}` | 查询单个原料批号（含当前锁定状态） |
| PUT | `/api/material-lots/{lot_no}` | **更正原料批号**（名称/过敏原），重算受影响批次及后续批次 |
| GET | `/api/lines` | 产线列表及各线批次数量 |

### 原料批号待检锁定

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/material-lots/{lot_no}/lock` | **按批号锁定**，请求体 `{"reason": "..."}`（必填）；返回当前状态、是否实际变化及受影响批次/核验单 |
| POST | `/api/material-lots/{lot_no}/unlock` | **解除锁定**（请求体与语义同锁定） |
| GET | `/api/material-lots/{lot_no}/lock` | 当前锁定状态 + 历次锁定/解除变更（版本、动作、原因、时间） |

锁定/解除响应：

```jsonc
{
  "lot_no": "RM-NUT-001",
  "locked": true,                 // 变化后的状态
  "changed": true,                // false=重复提交相同状态，未产生新变更
  "reason": "供应商通报待检风险",
  "lock_version": 1,              // 批号锁定状态版本（每次实际变化 +1）
  "updated_at": "2026-09-30T00:00:00+00:00",
  "affected_batches": [           // 直接使用该批号、出货资格版本递增的批次
    { "batch_no": "B002", "line_id": "L1", "seq": 2,
      "version": 3, "status": "BLOCKED", "locked_lots": ["RM-NUT-001"] }
  ],
  "affected_verifications": [     // 联动的核验单（已确认者已转 PENDING_REVIEW）
    { "verification_no": "SH-001", "status": "PENDING_REVIEW", "updated_at": "..." }
  ]
}
```

### 批次与裁决

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/batches` | **按产线顺序登记批次**，立即返回裁决（seq 自动取该线下一号） |
| GET | `/api/batches?line_id=L1` | 列出批次（可按产线过滤） |
| GET | `/api/batches/{batch_no}` | 查询批次及当前裁决快照 |
| PUT | `/api/batches/{batch_no}` | **更正批次**（用料/声明/清洁，任一可选），重算本批及后续批次 |
| PUT | `/api/batches/{batch_no}/cleaning` | 专用：仅更正开工前清洁核验确认清除的过敏原 |
| POST | `/api/batches/{batch_no}/cleaning/preview` | **只读预演**：正式清洁更正前推演同线该批及后续有效批次残留/实际/未声明/RELEASE/HOLD，及将转待复核的已确认核验单（不改任何数据） |
| POST | `/api/batches/{batch_no}/void` | **作废误登记批次**，请求体 `{"reason": "..."}`（必填非空）；保留可查、号不可复用，其后有效批次按序重算，已确认核验单转待复核；重复作废幂等 |
| POST | `/api/batches/{batch_no}/reassign` | **更正误归产线（换线）**，请求体 `{"target_line_id": "L2"}`（须为另一条已登记产线）；批次移至目标线末尾获新顺序号，原线序号留空，两线按序重算追加 `MOVE_BATCH` 版本，已确认核验单转待复核；直接用料不变、出货资格版本不递增 |
| GET | `/api/batches/{batch_no}/history` | 查询该批**历史裁决版本**（只追加，含触发原因与时间；逐版记录当时产线与顺序号），作废批次标明状态 |

### 批次作废

误登记、实际未投产的批次可按批次号作废（`POST /api/batches/{batch_no}/void`，
请求体 `{"reason": "非空原因"}`）。作废批次详情与列表项新增：

```jsonc
{
  "batch_no": "B2026002", "line_id": "L1", "seq": 2,
  "status": "VOIDED",            // ACTIVE=有效 / VOIDED=已作废
  "voided": true,
  "voided_reason": "计划误登记，该批实际未投产",
  "voided_at": "2026-09-30T00:00:00+00:00",
  "adjudication": { /* 冻结的裁决快照，不再产生新版本 */ },
  "shipment_eligibility": { /* 冻结的出货资格记录，仍可查询 */ }
}
```

作废响应（重复作废时 `changed=false`、`recomputed` 为空并回显首次原因）：

```jsonc
{
  "voided": { /* 同上：作废后的完整批次记录 */ },
  "changed": true,
  "reason": "计划误登记，该批实际未投产",
  "recomputed": [ /* 其后有效批次按序重算的裁决摘要（trigger=VOID_BATCH） */ ],
  "affected_verifications": [   /* 涉及作废批次或被重算批次的核验单（CONFIRMED 已转待复核） */
    { "verification_no": "SH-001", "status": "PENDING_REVIEW", "updated_at": "..." }
  ]
}
```

要点：作废保留原登记、历史裁决/资格与原因供查询，**批次号不可复用**；作废批次退出同线
残留链（其清洁视为未发生），不再参与当前资格统计、批号锁定联动与新核验；作废批次不得更正；
含作废批次的已有单据可查询但重新核验/确认均拒绝；历次确认快照不改。

### 误归产线更正（换线）

误归产线的有效批次可凭目标产线编号更正归属
（`POST /api/batches/{batch_no}/reassign`，请求体 `{"target_line_id": "L2"}`）。响应给出
批次新旧归属、原线/目标线两线重算结果及受影响核验单：

```jsonc
{
  "moved": { /* 更正后的完整批次记录：line_id=L2、seq=2，批次号与用料/声明/清洁不变 */ },
  "assignment": {
    "batch_no": "B2026002",
    "from_line_id": "L1",
    "from_seq": 2,          // 原线顺序号：留空不复用
    "to_line_id": "L2",
    "to_seq": 2             // 目标线末尾新顺序号
  },
  "source_line": {
    "line_id": "L1",
    "vacated_seq": 2,       // 原线留空的顺序号
    "recomputed": [ /* 原线从移出位置起重算的批次裁决摘要（trigger=MOVE_BATCH） */ ]
  },
  "target_line": {
    "line_id": "L2",
    "recomputed": [ /* 目标线从移入批次起重算的摘要，含移入批本身 */ ]
  },
  "recomputed": [ /* 两线重算摘要的合并列表（原线在前、目标线在后） */ ],
  "affected_verifications": [ /* 含被重算批次的核验单（CONFIRMED 已转 PENDING_REVIEW） */
    { "verification_no": "SH-001", "status": "PENDING_REVIEW", "updated_at": "..." }
  ]
}
```

要点：批次号不变；原线顺序号留空不复用、其余批次不重排；两线按各自有效批次顺序重算，
被重算批次各追加一个历史裁决版本，**历史版本中的原产线与顺序号不改**
（历史接口逐版返回 `line_id` / `seq`）；直接用料未变，**出货资格版本不递增**、
资格历史不追加；已确认核验单转待复核，历次确认快照保持原样；未知批次/目标线、已作废批次、
目标线与原线相同整体拒绝，失败不留下部分归属或裁决版本。

### 出货资格

每批另含独立于过敏原裁决的**出货资格**（批次详情、登记/更正响应均内联返回）：

```jsonc
"shipment_eligibility": {
  "version": 2,                    // 出货资格版本：批号锁定状态每次实际变化而递增
  "status": "BLOCKED",             // SHIPPABLE / BLOCKED
  "locked_lots": ["RM-NUT-001"],   // 直接使用且当前锁定的原料批号
  "reasons": [ /* 逐条原因，含锁定批号 */ ],
  "updated_at": "2026-09-30T00:00:00+00:00"
}
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/batches/{batch_no}/eligibility` | 查询单批当前出货资格及历次版本（含触发原因） |
| GET | `/api/eligibilities?line_id=L1&status=BLOCKED` | 逐批列出出货资格，可按产线 / `SHIPPABLE` / `BLOCKED` 过滤 |

### 裁决响应字段

批次详情/登记/更正/作废响应均为同一批次结构；作废时额外给出作废标记（有效批次为
`status="ACTIVE"`、`voided=false`，兼容既有字段）：

```jsonc
{
  "batch_no": "B2026003", "line_id": "L1", "seq": 3,
  "status": "ACTIVE",            // ACTIVE=有效 / VOIDED=已作废
  "voided": false,
  "voided_reason": null,         // 作废时为非空原因
  "voided_at": null,
  "material_lots": [],
  "declared_allergens": [],
  "cleaned_allergens": [],
  "adjudication": {
    "version": 1,
    "actual_allergens": ["peanut"],      // 实际可能含有的过敏原
    "undeclared_allergens": ["peanut"],  // 未声明项（非空即扣留）
    "disposition": "HOLD",               // RELEASE / HOLD
    "reasons": [ /* 逐条原因，含过敏原与来源说明 */ ],
    "sources": { "peanut": ["上一批残留在共用产线、本次清洁未确认清除"] },
    "decided_at": "2026-09-30T00:00:00+00:00"
  }
}
```

更正接口额外返回 `recomputed`：本次按序重算的批次裁决摘要列表；
原料批号更正会列出各产线受影响批次。
更正若重算了核验单所含批次，响应还附带 `affected_verifications`（含各核验单更正后的状态）。
批次更正响应另含 `shipment_eligibility`（本次是否递增资格版本、版本号、状态、锁定批号）。

### 开工前清洁更正预演（只读）

`POST /api/batches/{batch_no}/cleaning/preview`，请求体与正式清洁更正完全一致：

```json
{ "cleaned_allergens": ["peanut"] }
```

响应（`200`）：

```jsonc
{
  "simulation": "CLEANING_CORRECTION_PREVIEW",
  "read_only": true,
  "correction_target": {
    "batch_no": "B2026002", "line_id": "L1", "seq": 2,
    "current_cleaned_allergens": [],            // 当前清洁记录
    "proposed_cleaned_allergens": ["peanut"]    // 拟更正的完整集合
  },
  "line_id": "L1",
  "start_seq": 2,
  "batches": [                                  // 从拟更正批（含）起的有效批次，按 seq
    {
      "batch_no": "B2026002", "line_id": "L1", "seq": 2,
      "status": "ACTIVE", "voided": false,
      "is_correction_target": true,
      "proposed_cleaned_allergens": ["peanut"],
      "current": {                              // 当前裁决（快照现状）
        "version": 3,
        "cleaned_allergens": [],
        "carried_in_allergens": ["peanut"],     // 上一有效批实际过敏原（带入）
        "residual_allergens": ["peanut"],       // 带入 − 本次清洁确认清除
        "from_material_allergens": [],
        "actual_allergens": ["peanut"],
        "undeclared_allergens": ["peanut"],
        "disposition": "HOLD",
        "reasons": [ /* ... */ ], "sources": { /* ... */ }
      },
      "preview": {                              // 拟更正落库后的预演裁决
        "version": 4,                           // = 当前版本 + 1（正式更正将追加 CORRECT_BATCH）
        "trigger_if_applied": "CORRECT_BATCH",
        "cleaned_allergens": ["peanut"],
        "carried_in_allergens": ["peanut"],
        "residual_allergens": [],
        "from_material_allergens": [],
        "actual_allergens": [],
        "undeclared_allergens": [],
        "disposition": "RELEASE",
        "reasons": [ /* ... */ ], "sources": { /* ... */ }
      },
      "changed": true,                          // 残留/实际/未声明/结论是否有任一变化
      "change_reasons": [                       // 逐条变化原因；无变化时给出仅追加版本说明
        { "code": "CLEANING_SET_CHANGED", "from": [], "to": ["peanut"],
          "added": ["peanut"], "removed": [], "message": "..." },
        { "code": "RESIDUAL_CHANGED", "from": ["peanut"], "to": [], "message": "..." },
        { "code": "ACTUAL_ALLERGENS_CHANGED", "/* ... */": "..." },
        { "code": "UNDECLARED_ALLERGENS_CHANGED", "/* ... */": "..." },
        { "code": "DISPOSITION_CHANGED", "from": "HOLD", "to": "RELEASE", "message": "..." }
      ],
      "in_version_scope": true,                 // 结论相同也为 true：正式更正必追加版本
      "shipment_eligibility": {                 // 待检锁定不参与过敏原推演
        "version": 2,
        "would_advance_if_applied": false,      // 清洁更正不改直接用料，资格版本不递增
        "current": { /* 当前出货资格现状：status/locked_lots/reasons */ }
      }
    }
    // …后续有效批次同样逐批给出（即使预演结论与当前相同）
  ],
  "skipped_voided_batches": [                   // 从该批起同线已作废批次（逐批推演时跳过）
    { "batch_no": "B2026005", "line_id": "L1", "seq": 5, "status": "VOIDED",
      "voided_reason": "计划误登记，实际未投产", "voided_at": "...",
      "reason": "已作废批次不参与同线残留传递，预演逐批推演时自动跳过" }
  ],
  "affected_verifications": [                   // 仅当前 CONFIRMED 的单据；正式更正后将转待复核
    {
      "verification_no": "SH-001",
      "current_status": "CONFIRMED",
      "status_if_applied": "PENDING_REVIEW",
      "updated_at": "...",
      "reason": "所含批次将因清洁更正正式落库而追加裁决版本（CORRECT_BATCH），即使裁决结论不变，已确认核验单也将自动转待复核，重新核验后才能再次确认",
      "batches": [                              // 按单据内顺序，仅列处于版本影响范围的批次
        { "position": 1, "batch_no": "B2026003", "line_id": "L1", "seq": 3,
          "current_version": 2, "current_disposition": "RELEASE",
          "preview_version": 3, "preview_disposition": "RELEASE",
          "preview_undeclared_allergens": [],
          "disposition_changed": false }
      ]
    }
  ]
}
```

变化原因 `change_reasons[].code`：

| code | 含义 |
| --- | --- |
| `CLEANING_SET_CHANGED` | 仅目标批：清洁确认清除集合将全量替换（含 `added` / `removed`） |
| `RESIDUAL_CHANGED` | 清洁后产线残留过敏原集合变化 |
| `ACTUAL_ALLERGENS_CHANGED` | 实际可能含有的过敏原变化 |
| `UNDECLARED_ALLERGENS_CHANGED` | 未声明过敏原变化 |
| `DISPOSITION_CHANGED` | RELEASE ↔ HOLD 翻转 |
| `NO_ADJUDICATION_CONTENT_CHANGE` | 裁决内容无变化，但正式更正仍追加版本、仍属版本影响范围 |

要点：预演**严格只读**（清洁记录、裁决/资格版本、核验单状态、历史均不变）；
从拟更正批起跳过作废批次逐批推演；后续有效批次即使结论相同仍在版本影响范围内；
待检锁定只影响出货资格、不参与过敏原推演；预演结果与随后正式 `PUT .../cleaning`
落库的重算结果一致；未知/已作废批次、非法过敏原代码整体报错（错误码与正式更正一致）。

### 出货核验单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/verifications` | 创建核验单（草稿）：唯一单号 + 非空、无重复的已登记批次号列表，逐批保存当前裁决版本/结论与出货资格版本/状态 |
| GET | `/api/verifications?status=DRAFT` | 列出核验单，可按 `DRAFT` / `PENDING_REVIEW` / `CONFIRMED` 过滤 |
| GET | `/api/verifications/{verification_no}` | 核验单详情：状态、修订号 `revision`、逐批记录（裁决与出货资格的快照/当前版本/变化原因）、历次确认快照 |
| POST | `/api/verifications/{verification_no}/reverify` | **重新核验**：刷新逐批快照并回到草稿（仅 DRAFT / PENDING_REVIEW 可调用） |
| POST | `/api/verifications/{verification_no}/revise` | **修订批次清单**：凭所见修订号提交完整、有序的新批次清单（仅 DRAFT / PENDING_REVIEW）；同事务重取逐批快照、回到草稿并推进修订号 |
| POST | `/api/verifications/{verification_no}/confirm` | **确认**：原子核对逐批裁决版本、出货资格版本与当前锁定状态 |

### 装车交接

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/handovers` | **装车交接（整单发运）**：凭唯一交接号、已确认核验单号、读取时修订号；单事务占用全单批次并冻结单号/修订号/逐批确认快照/交接时间 |
| GET | `/api/handovers?verification_no=` | 列出交接记录（可按核验单号过滤） |
| GET | `/api/handovers/{handover_no}` | 交接详情（冻结快照与交接时间） |
| GET | `/api/batches/{batch_no}/handover` | 批次交接归属（已发运回显交接记录，否则 `handover=null`） |

**状态机**：

```
DRAFT ──confirm 成功──▶ CONFIRMED
  ▲                        │ 所含批次产生新裁决版本或出货资格版本变化（即使仍为 RELEASE/SHIPPABLE）
  └── reverify ── PENDING_REVIEW ◀┘
```

> 草稿/待复核还可通过 **revise（修订批次清单）** 用一份完整新清单替换成员，
> 单据回到 `DRAFT` 并推进修订号；已确认单据必须先（因版本变化自动）转待复核才能修订。

#### 修订号（revision，乐观并发）

每张核验单维护只增的**修订号**：创建为 `1`；**重新核验、修订批次清单、确认**等
单据快照写入各使修订号 `+1`（详情与列表均回显 `revision`）。批次更正/批号锁定只
推进**逐批**裁决/出货资格版本，不推进单据修订号（草稿快照本就待刷新；若单据已
确认则自动转待复核，待重新核验/修订后再确认）。

#### 出货前批次清单修订（revise）

出货前装运批次发生变化时，工作人员**先查询单据**看到当前修订号与成员顺序，再凭
**所见修订号**提交完整、有序的新批次清单
（`POST /api/verifications/{verification_no}/revise`）：

```json
{ "expected_revision": 3, "batch_nos": ["B003", "B001"] }
```

- **同事务重取快照**：`expected_revision` 校验通过后，在**同一 `BEGIN IMMEDIATE`
  事务内**删除旧逐批记录、按新顺序逐批重取**当前**裁决版本/结论与出货资格版本/状态，
  单据回到 `DRAFT`、修订号 `+1`。并发的批次更正/批号锁定由进程写锁串行化——
  修订事务期间其它写操作要么已提交可见、要么尚未提交，**逐批快照全部取自同一事务
  快照**，不会一部分取更正前、一部分取更正后。
- **可移除已作废批次**：旧清单中的批次若在此期间被作废，可通过修订把它移出
  （含作废批次的单据重新核验/确认仍被拒，修订是恢复单据可用的途径）；
  但**新清单本身必须全部有效**——任何已作废批次出现在新清单中整体拒绝。
- **确认历史不动**：修订只替换“当前成员与逐批快照”，
  `verification_confirmation(_batch)` 历次确认及其当时逐批快照**始终保留**，
  即使旧批次已不在现行清单；再次确认追加而非覆盖。
- **已确认不可修订**：`CONFIRMED` 单据直接修订返回 409
  `VERIFICATION_ALREADY_CONFIRMED`（须等其因版本变化转待复核后再修订）。
- **整体拒绝（任一校验失败，当前成员与快照不变、修订号不变）**：

| 场景 | HTTP | error.code |
| --- | --- | --- |
| 核验单不存在 | 404 | `VERIFICATION_NOT_FOUND` |
| 修订已确认单据 | 409 | `VERIFICATION_ALREADY_CONFIRMED` |
| 所见修订号过期（与当前修订号不一致） | 409 | `VERIFICATION_REVISION_CONFLICT`（`details` 含 `submitted_revision` / `current_revision`） |
| 新清单含未登记批次 | 400 | `UNKNOWN_BATCH` |
| 新清单内批次号重复 | 400 | `DUPLICATE_BATCH_IN_VERIFICATION` |
| 新清单含已作废批次 | 400 | `BATCH_VOIDED` |
| 清单元素为空/非字符串 | 400 | `INVALID_PAYLOAD` |
| `batch_nos` 为空 / 缺字段 / `expected_revision` 缺失或 < 1 | 422 | FastAPI 模型校验 |

冲突后客户端应重新 `GET` 单据取得最新 `revision` 与成员顺序，核对差异后用新修订号
重新提交完整清单。


- 创建即 `DRAFT`，修订号 `revision=1`，逐批冻结当时的裁决 `version`/`disposition` 与出货资格
  `shipment_eligibility.version`/`status`（双快照）。
- 确认是一次**原子核对**，三项须逐批全部满足：
  1. 裁决快照版本等于当前裁决版本，且结论为 `RELEASE`；
  2. 出货资格快照版本等于当前出货资格版本；
  3. 当前没有直接使用的待检锁定批号（即使过敏原裁决为 RELEASE）。
  任一不符返回 **422 `VERIFICATION_NOT_CONFIRMABLE`**，`error.details[]` 列出**全部**不符批次：
  - `STALE_SNAPSHOT`：裁决快照版本已落后（含 `snapshot_version` / `current_version` / `change_reasons`）；
  - `BATCH_ON_HOLD`：裁决为 HOLD，**HOLD 批次始终不能确认**（含未声明过敏原清单）；
  - `STALE_ELIGIBILITY`：出货资格快照版本已落后（含双版本号与 `change_reasons`）；
  - `MATERIAL_LOT_LOCKED`：当前直接使用的原料批号处于待检锁定（含 `locked_lots`）。
  校验失败不写入任何确认记录，单据保持未确认。
- `CONFIRMED` 单据所含批次一旦产生新裁决版本**或出货资格版本变化**，立即自动转 `PENDING_REVIEW`；
  待复核单据必须先 `reverify`（刷新两套快照、回到 `DRAFT`）才能再次确认。
- 每次成功确认都向**只追加**的确认历史写入一份逐批快照不可变副本（含裁决与出货资格）；重新确认不覆盖历史。
- 同一批次可同时被多张核验单关联；草稿/待复核单据不会被更正或锁定联动转态（其快照本就待刷新）。

核验单创建请求体：

```json
{ "verification_no": "SH-20260930-01", "batch_nos": ["B001", "B003"] }
```

详情中每批 `batches[]` 记录：

```jsonc
{
  "position": 1, "batch_no": "B001", "line_id": "L1", "seq": 1,
  "status": "ACTIVE",            // 该批当前状态：ACTIVE / VOIDED
  "voided": false,               // 作废批次为 true，并带 voided_reason/voided_at
  "voided_reason": null,
  "voided_at": null,
  "snapshot": { "version": 1, "disposition": "RELEASE", /* actual/undeclared/reasons */ },
  "current":  { "version": 3, "disposition": "RELEASE", /* ...完整当前裁决... */ },
  "changed": true,                                   // 当前裁决版本 != 快照版本
  "change_reasons": [                                // 快照版本之后每个裁决版本一条
    { "code": "ADJUDICATION_VERSION_CHANGED",
      "from_version": 1, "to_version": 2, "trigger": "CORRECT_BATCH",
      "message": "裁决版本 v2（批次更正重算）晚于快照版本 v1，结论 RELEASE",
      "decided_at": "2026-09-30T00:00:00+00:00" }
  ],
  "shipment_snapshot": { "version": 1, "status": "SHIPPABLE", "locked_lots": [], "reasons": [/*...*/] },
  "shipment_current":  { "version": 2, "status": "BLOCKED",  "locked_lots": ["RM-NUT-001"], "reasons": [/*...*/] },
  "shipment_changed": true,                          // 当前出货资格版本 != 快照版本
  "shipment_change_reasons": [                       // 快照版本之后每个出货资格版本一条
    { "code": "SHIP_ELIGIBILITY_VERSION_CHANGED",
      "from_version": 1, "to_version": 2, "trigger": "LOT_LOCK",
      "message": "出货资格版本 v2（原料批号待检锁定）晚于快照版本 v1，资格 BLOCKED，锁定批号: RM-NUT-001",
      "changed_at": "2026-09-30T00:00:00+00:00" }
  ]
}
```

`confirmations[]` 为历次确认（`confirmation_id`、`confirmed_at` 与当时逐批的
裁决版本/结论、出货资格版本/状态快照）。

单据顶层另含 `contains_voided_batches`（是否含已作废批次）与 `voided_batches`
（作废批次号列表，按单据内顺序）；含作废批次的单据可查询，但重新核验与确认均返回
409 `VERIFICATION_CONTAINS_VOIDED_BATCH`。

核验单相关拒绝码：

| 场景 | HTTP | error.code |
| --- | --- | --- |
| 核验单号重复 | 409 | `DUPLICATE_VERIFICATION` |
| 列表含未登记批次 | 400 | `UNKNOWN_BATCH` |
| 列表内批次号重复 | 400 | `DUPLICATE_BATCH_IN_VERIFICATION` |
| 单号为空 / 列表非数组 | 400 | `INVALID_PAYLOAD`（空列表为 422 模型校验） |
| 核验单不存在 | 404 | `VERIFICATION_NOT_FOUND` |
| 已确认单据再次确认或重新核验 | 409 | `VERIFICATION_ALREADY_CONFIRMED` |
| 修订已确认单据的批次清单 | 409 | `VERIFICATION_ALREADY_CONFIRMED` |
| 修订时所见修订号已过期 | 409 | `VERIFICATION_REVISION_CONFLICT` |
| 修订新清单含未登记/重复/已作废批次 | 400 | `UNKNOWN_BATCH` / `DUPLICATE_BATCH_IN_VERIFICATION` / `BATCH_VOIDED` |
| 修订清单为空或 `expected_revision` 缺失/非法 | 422/400 | FastAPI 模型校验 / `INVALID_PAYLOAD` |
| 含已作废批次的单据重新核验/确认 | 409 | `VERIFICATION_CONTAINS_VOIDED_BATCH`（`details` 列作废批次及原因） |
| 确认时存在不符批次 | 422 | `VERIFICATION_NOT_CONFIRMABLE`（`details` 逐批说明） |

### 装车交接（整单发运）

仓库完成装车交接时，凭**唯一交接号、已确认核验单号及读取时修订号**登记整单发运：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/handovers` | **装车交接**：请求体 `{"handover_no","verification_no","expected_revision"}`；单事务占用全单批次并冻结单号/修订号/逐批确认快照/交接时间（201） |
| GET | `/api/handovers` | 列出全部交接记录（可 `?verification_no=` 过滤；列表不展开逐批快照） |
| GET | `/api/handovers/{handover_no}` | 交接详情：冻结的核验单号、修订号、逐批确认快照与交接时间 |
| GET | `/api/batches/{batch_no}/handover` | **逐批交接归属**：已发运回显交接记录，未发运 `handover=null`；未知批次 404 |

请求体：

```json
{
  "handover_no": "HO-20261002-01",
  "verification_no": "SH-001",
  "expected_revision": 2
}
```

规则：

- **只交仍为已确认且修订号匹配的单据**：单据当前必须为 `CONFIRMED`，且
  `expected_revision` 等于读取时的单据 `revision`（确认会推进修订号；交接
  **不推进**单据修订号，也不改单据状态）。
- **逐批以“最近一次确认快照”为准**：单内每个批次（按最近一次确认清单顺序）须
  - 批次仍**有效**（未作废）；
  - 当前**裁决版本**与确认快照一致，且结论为 **RELEASE**；
  - 当前**出货资格版本**与确认快照一致，且结论为 **SHIPPABLE**。
- **批次全局唯一占用**：任一批次已在**其他交接记录**中发运，整单拒绝
  （`shipment_handover_batch.batch_no` 全局唯一，INSERT 阶段亦为并发兜底）。
- **单事务、整单原子**：全部校验在同一 `BEGIN IMMEDIATE` 事务内完成，逐批结果
  取自同一事务快照；校验通过才一次性写入交接主记录与全部逐批记录。任一失败
  整体回滚，**不留下部分占用**。
- **只追加、不可改、可追溯**：交接记录冻结交接时事实；此后对核验单的更正重算、
  自动转待复核、重新核验/修订清单/再次确认，以及批次更正/作废，**均不改写**
  交接记录。交接记录不回显单据当前状态；每批的交接归属始终可通过
  `GET /api/batches/{batch_no}/handover` 追溯。
- 同一批次仍可同时属于多张核验单；但只有尚未在任何交接记录中发运时才能交接。
  含已发运批次的单据整单拒绝后，可先通过**修订清单**（须单据已转待复核）移除该批，
  再重新确认、交接其余批次。

交接响应（`201`，GET 详情同构）：

```jsonc
{
  "handover_no": "HO-20261002-01",
  "verification_no": "SH-001",
  "confirmation_id": 2,          // 最近一次确认（交接冻结其逐批快照）
  "verification_revision": 2,   // 交接时冻结的单据修订号
  "handed_over_at": "2026-10-02T08:00:00+00:00",
  "batches": [                  // 按确认清单顺序，逐批冻结快照
    {
      "position": 1, "batch_no": "B001", "line_id": "L1", "seq": 1,
      "status": "ACTIVE", "voided": false, "voided_reason": null, "voided_at": null,
      "snapshot": { "version": 3, "disposition": "RELEASE", /* actual/undeclared/reasons */ },
      "shipment_snapshot": { "version": 2, "status": "SHIPPABLE",
                             "locked_lots": [], "reasons": [/* ... */] }
    }
  ]
}
```

交接相关拒绝码（全部为整单拒绝，事务回滚，不占用任何批次）：

| 场景 | HTTP | error.code |
| --- | --- | --- |
| 交接号重复 | 409 | `DUPLICATE_HANDOVER` |
| 核验单不存在 | 404 | `VERIFICATION_NOT_FOUND` |
| 单据不是 CONFIRMED（DRAFT / PENDING_REVIEW） | 409 | `VERIFICATION_NOT_CONFIRMED`（`details` 含当前状态/修订号） |
| 读取时修订号与当前修订号不一致 | 409 | `VERIFICATION_REVISION_CONFLICT`（`details` 含 `submitted_revision` / `current_revision`） |
| 任一逐批不合格（作废/裁决版本漂移/HOLD/资格版本漂移/BLOCKED） | 409 | `HANDOVER_BATCH_NOT_READY`（`details` 逐批列出全部不合格批次） |
| 任一批次已在其他交接记录发运 | 409 | `HANDOVER_BATCH_NOT_READY`（`details[].reason_code=BATCH_ALREADY_SHIPPED`，含 `shipped_in_handover`） |
| 交接记录不存在 | 404 | `HANDOVER_NOT_FOUND` |
| 交接号/核验单号为空白 | 400 | `INVALID_PAYLOAD` |
| 缺字段 / `expected_revision` < 1 | 422 | FastAPI 模型校验 |

> 正常流程下，已确认单据所含批次一旦产生新裁决/资格版本，单据会立即自动转
> 待复核，因此“仍为已确认且修订号匹配”时逐批版本必然一致；交接接口中的逐批
> 版本/结论校验是同一事务内的纵深防御，`details` 会一次性列出全部不合格批次。

---

## 8. curl 调用示例

```bash
# 1) 登记原料批号
curl -s -X POST http://127.0.0.1:8080/api/material-lots \
  -H 'Content-Type: application/json' \
  -d '{"lot_no":"RM-MILK-002","material":"全脂奶粉","allergens":["milk"]}'

# 2) 登记第 1 批：牛奶巧克力，声明 milk —— 放行
curl -s -X POST http://127.0.0.1:8080/api/batches \
  -H 'Content-Type: application/json' \
  -d '{"batch_no":"B001","line_id":"L1","material_lots":["RM-MILK-002"],
       "declared_allergens":["milk"],"cleaned_allergens":[]}'

# 3) 登记第 2 批：使用含花生的原料、只清洁 milk、只声明 milk —— peanut 未声明，扣留
curl -s -X POST http://127.0.0.1:8080/api/batches \
  -H 'Content-Type: application/json' \
  -d '{"batch_no":"B002","line_id":"L1","material_lots":["RM-NUT-001"],
       "declared_allergens":["milk"],"cleaned_allergens":["milk"]}'

# 4) 第 3 批无过敏原原料且未清洁 —— 继承上批（被扣留批）残留 peanut，扣留
curl -s -X POST http://127.0.0.1:8080/api/batches \
  -H 'Content-Type: application/json' \
  -d '{"batch_no":"B003","line_id":"L1","material_lots":[],
       "declared_allergens":[],"cleaned_allergens":[]}'

# 5) 更正 B003 开工前清洁核验：确认清除 peanut —— B003 及其后续批次自动重算
curl -s -X PUT http://127.0.0.1:8080/api/batches/B003/cleaning \
  -H 'Content-Type: application/json' \
  -d '{"cleaned_allergens":["peanut"]}'

# 5b) 正式更正前先“只读预演”：推演同线该批及后续有效批次的残留/实际/未声明/RELEASE|HOLD，
#     并列出将因裁决版本追加而转待复核的已确认核验单（不改动任何数据）
curl -s -X POST http://127.0.0.1:8080/api/batches/B003/cleaning/preview \
  -H 'Content-Type: application/json' \
  -d '{"cleaned_allergens":["peanut"]}'

# 6) 更正原料批号过敏原（例如复检发现还含 sesame）—— 所有引用批次按线重算
curl -s -X PUT http://127.0.0.1:8080/api/material-lots/RM-NUT-001 \
  -H 'Content-Type: application/json' \
  -d '{"allergens":["peanut","sesame"]}'

# 7) 查询历史裁决版本
curl -s http://127.0.0.1:8080/api/batches/B003/history

# 8) 创建出货核验单（草稿）：关联两个已登记批次，逐批冻结裁决版本与结论
curl -s -X POST http://127.0.0.1:8080/api/verifications \
  -H 'Content-Type: application/json' \
  -d '{"verification_no":"SH-001","batch_nos":["B001","B003"]}'

# 9) 确认（原子校验：快照仍当前版本且全部 RELEASE；否则 422 返回不符批次明细）
curl -s -X POST http://127.0.0.1:8080/api/verifications/SH-001/confirm

# 10) 已确认单据所含批次被更正重算 -> 自动转 PENDING_REVIEW；重新核验刷新快照
curl -s -X POST http://127.0.0.1:8080/api/verifications/SH-001/reverify

# 11) 查询核验单（状态、逐批快照/当前版本/变化原因、历次确认快照）
curl -s http://127.0.0.1:8080/api/verifications/SH-001
curl -s 'http://127.0.0.1:8080/api/verifications?status=PENDING_REVIEW'

# 12) 原料批号出现待检风险：锁定并填写原因（直接使用批次出货资格版本递增；
#     过敏原裁决仍为 RELEASE 也不得通过出货核验；已确认核验单转 PENDING_REVIEW）
curl -s -X POST http://127.0.0.1:8080/api/material-lots/RM-NUT-001/lock \
  -H 'Content-Type: application/json' \
  -d '{"reason":"供应商通报黄曲霉毒素风险，待复检"}'

# 13) 查询批号当前状态与历次变更
curl -s http://127.0.0.1:8080/api/material-lots/RM-NUT-001/lock

# 14) 逐批查询出货资格（可按产线 / SHIPPABLE / BLOCKED 过滤）
curl -s http://127.0.0.1:8080/api/batches/B002/eligibility
curl -s 'http://127.0.0.1:8080/api/eligibilities?line_id=L1&status=BLOCKED'

# 15) 复检合格后解除（再次提交相同“锁定”不会产生新变更）
curl -s -X POST http://127.0.0.1:8080/api/material-lots/RM-NUT-001/unlock \
  -H 'Content-Type: application/json' \
  -d '{"reason":"复检合格，解除待检"}'

# 16) 核验单重新核验（刷新裁决+出货资格双快照）后再次确认
curl -s -X POST http://127.0.0.1:8080/api/verifications/SH-001/reverify
curl -s -X POST http://127.0.0.1:8080/api/verifications/SH-001/confirm

# 17) 作废误登记、实际未投产的批次（必须填写非空原因；保留可查、号不可复用；
#     作废批次退出同线残留链，其后有效批次按序重算，已确认核验单转待复核）
curl -s -X POST http://127.0.0.1:8080/api/batches/B002/void \
  -H 'Content-Type: application/json' \
  -d '{"reason":"计划误登记，该批实际未投产"}'

# 18) 查询作废批次（详情/历史均标明 VOIDED；裁决与资格快照冻结保留）
curl -s http://127.0.0.1:8080/api/batches/B002
curl -s http://127.0.0.1:8080/api/batches/B002/history

# 19) 重复作废：幂等返回既有结果（changed=false），不重算
curl -s -X POST http://127.0.0.1:8080/api/batches/B002/void \
  -H 'Content-Type: application/json' \
  -d '{"reason":"再次提交作废"}'

# 20) 批次误归产线更正：凭有效批次号与另一条已登记产线编号移至目标线末尾
#     （批次号不变、原线序号留空、两线按序重算追加 MOVE_BATCH 版本；
#      直接用料不变，出货资格版本不递增；已确认核验单转待复核）
curl -s -X POST http://127.0.0.1:8080/api/batches/B003/reassign \
  -H 'Content-Type: application/json' \
  -d '{"target_line_id":"L2"}'

# 21) 查询历史裁决版本：v1 保留原产线/顺序号，v2 为换线后（MOVE_BATCH）
curl -s http://127.0.0.1:8080/api/batches/B003/history

# 22) 出货前装运批次变化：先查单据拿“所见修订号”，再提交完整、有序的新批次清单
#     （同事务重取逐批当前裁决/出货资格快照；可移除旧清单中已作废批次；
#      回到草稿并推进修订号；已确认单据不可直接修订）
curl -s http://127.0.0.1:8080/api/verifications/SH-001
curl -s -X POST http://127.0.0.1:8080/api/verifications/SH-001/revise \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":1,"batch_nos":["B003","B001"]}'

# 23) 过期修订号（并发已有重新核验/修订/确认推进过修订号）-> 409 整体拒绝，
#     error.details 给出 submitted_revision/current_revision；重新查询后用新号再提交
curl -s -X POST http://127.0.0.1:8080/api/verifications/SH-001/revise \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":1,"batch_nos":["B001"]}'

# 24) 仓库完成装车交接：先 GET 单据拿到“读取时修订号”，再凭唯一交接号登记整单发运
#     （单据须仍为 CONFIRMED 且修订号匹配；逐批 RELEASE/SHIPPABLE；全单批次单事务占用）
curl -s http://127.0.0.1:8080/api/verifications/SH-001
curl -s -X POST http://127.0.0.1:8080/api/handovers \
  -H 'Content-Type: application/json' \
  -d '{"handover_no":"HO-001","verification_no":"SH-001","expected_revision":2}'

# 25) 交接查询：详情（冻结快照）/ 列表 / 每批交接归属（未发运 handover=null）
curl -s http://127.0.0.1:8080/api/handovers/HO-001
curl -s 'http://127.0.0.1:8080/api/handovers?verification_no=SH-001'
curl -s http://127.0.0.1:8080/api/batches/B001/handover

# 26) 交接后单据更正/转待复核不影响交接记录；重复交接号、未知单据、状态/版本冲突、
#     任一批次已在其他交接发运均 409/404 整体拒绝，不留下部分占用
curl -s -X POST http://127.0.0.1:8080/api/handovers \
  -H 'Content-Type: application/json' \
  -d '{"handover_no":"HO-002","verification_no":"SH-002","expected_revision":2}'
```

更多可运行示例见 [`examples/run_demo.py`](examples/run_demo.py)（包含完整的
登记 → 扣留传递 → 清洁更正重算 → 历史查询 → 各类拒绝场景）；
出货核验单全流程示例见 [`examples/verification_demo.py`](examples/verification_demo.py)
（创建 → HOLD 拦截 → 确认 → 更正转待复核 → 重新核验 → 二次确认与历史快照）；
原料批号待检锁定全流程示例见 [`examples/lot_lock_demo.py`](examples/lot_lock_demo.py)
（锁定 → 出货资格版本递增 → 残留不连带 → 确认拦截 → 待复核 → 解除 → 重新核验 → 二次确认）；
误登记批次作废全流程示例见 [`examples/batch_void_demo.py`](examples/batch_void_demo.py)
（作废保留 → 号不可复用/不得更正 → 退出残留链按序重算 → 已确认单据转待复核 →
含作废批次单据拒绝重新核验/确认 → 重复作废幂等 → 查询标明状态）；
误归产线更正（换线）全流程示例见 [`examples/batch_reassign_demo.py`](examples/batch_reassign_demo.py)
（拒绝场景 → 换线新旧归属/原线留空 → 两线重算与历史版本产线序号不改 →
出货资格版本不递增 → 已确认单据转待复核且确认快照原样 → 顺序号不复用 →
重新核验后二次确认与历史快照保留）；
出货前批次清单修订全流程示例见 [`examples/verification_revise_demo.py`](examples/verification_revise_demo.py)
（凭修订号替换完整有序清单 → 同事务重取快照 → 过期修订号并发拒绝 →
未知/重复/空清单/含作废批次整体拒绝 → 借修订移除已作废批次 →
历次确认快照保留 → 已确认单据不可修订）；
开工前清洁更正预演示例见 [`examples/cleaning_preview_demo.py`](examples/cleaning_preview_demo.py)
（已确认单据与整链 RELEASE 基线 → 预演改为不清洁逐批 HOLD 及变化原因 →
结论相同仍属版本影响范围 → 只读性核对 → 未知批次/非法过敏原拒绝 →
正式更正落库与预演一致并使单据转待复核）；
装车交接（整单发运）全流程示例见 [`examples/handover_demo.py`](examples/handover_demo.py)
（已确认单据 + 读取时修订号登记整单发运 → 冻结单号/修订号/逐批确认快照 →
交接详情/列表/每批交接归属 → 交接后更正转待复核不改写交接记录 →
重复交接号/未知单据/状态与版本冲突/批次已发运整体拒绝不留部分占用）。
示例脚本可重复调用其中的“登记原料批号”接口（重复时自动跳过）；批次号固定，
重复运行前如需重置可 `docker compose down -v` 后重新启动，或删除本地 `data/` 目录。
`handover_demo.py` 使用时间戳后缀批号与独立产线，可对同一服务重复运行。

---

## 9. 项目结构

```
.
├── app/
│   ├── config.py        # 环境变量配置（DB_PATH / HOST / PORT / SEED_DEMO_DATA）
│   ├── allergens.py     # 14 项过敏原受控字典
│   ├── errors.py        # 业务错误（code + HTTP 状态码）
│   ├── db.py            # SQLite 连接、建表、事务、初始化/种子
│   ├── service.py       # 核心规则：传递/清洁/裁决、更正重算、历史版本、出货核验单
│   ├── schemas.py       # 请求模型
│   ├── main.py          # FastAPI 路由
│   └── __main__.py      # python -m app 入口
├── examples/run_demo.py          # 裁决/更正/历史端到端调用示例
├── examples/verification_demo.py # 出货核验单端到端调用示例
├── examples/lot_lock_demo.py     # 原料批号待检锁定/出货资格端到端调用示例
├── examples/batch_void_demo.py   # 误登记批次作废端到端调用示例
├── examples/batch_reassign_demo.py # 误归产线更正（换线）端到端调用示例
├── examples/verification_revise_demo.py # 出货前批次清单修订（乐观修订号）端到端调用示例
├── examples/cleaning_preview_demo.py # 开工前清洁更正预演（只读）端到端调用示例
├── examples/handover_demo.py      # 装车交接（整单发运/冻结快照/唯一占用/交接归属）端到端调用示例
├── tests/test_service.py         # pytest 测试（裁决/更正/历史，20 项）
├── tests/test_verification.py    # pytest 测试（出货核验单，15 项）
├── tests/test_verification_revise.py # pytest 测试（批次清单修订/修订号/并发一致性/旧库升级，19 项）
├── tests/test_lot_lock.py        # pytest 测试（批号锁定/出货资格/旧库升级，18 项）
├── tests/test_batch_void.py      # pytest 测试（批次作废/重算联动/核验单拒绝/旧库升级，23 项）
├── tests/test_batch_reassign.py  # pytest 测试（误归产线换线/两线重算/历史归属/资格不递增/核验单联动/原子性，26 项）
├── tests/test_cleaning_preview.py # pytest 测试（清洁更正预演：逐批推演/版本影响范围/跳过作废批/锁定不参与/只读性/与正式更正一致/拒绝，20 项）
├── tests/test_handover.py        # pytest 测试（装车交接：整单发运/冻结快照/唯一占用/交接归属/并发一致/旧库升级，23 项）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 10. 数据库表（SQLite）

- `allergen`：过敏原字典
- `material_lot`：原料批号 → 名称、过敏原集合
- `line` / `batch` / `batch_material`：产线、批次（含 `line_id+seq` 唯一顺序、
  `status`/`voided_reason`/`voided_at` 作废标记）、批次用料关联
- `adjudication`：每批当前裁决快照（作废后冻结保留，不再更新）
- `adjudication_version`：只追加的历史裁决版本（`REGISTER` / `CORRECT_BATCH` / `CORRECT_LOT` /
  `VOID_BATCH` / `MOVE_BATCH`；每版记录裁决当时的 `line_id` / `seq`，换线不改写历史版本）
- `material_lot_lock` / `material_lot_lock_version`：原料批号当前待检锁定状态与只追加的
  锁定/解除历史（`LOCK` / `UNLOCK`）
- `shipment_eligibility` / `shipment_eligibility_version`：每批当前出货资格与只追加历史
  （`REGISTER` / `LOT_LOCK` / `LOT_UNLOCK` / `CORRECT_BATCH` / `UPGRADE_INIT`）
- `verification`：出货核验单（`DRAFT` / `PENDING_REVIEW` / `CONFIRMED`；
  `revision` 单据修订号，创建为 1，重新核验/批次清单修订/确认等单据快照写入时递增，
  用于修订的乐观并发校验）
- `verification_batch`：核验单逐批快照（创建/重新核验/批次清单修订时的裁决版本/结论
  与出货资格版本/状态，按提交顺序保序；修订时整体重写）
- `verification_confirmation` / `verification_confirmation_batch`：只追加的历次确认及其逐批快照副本
- `shipment_handover` / `shipment_handover_batch`：装车交接主记录（唯一交接号、
  核验单号、最近一次确认 id、冻结的单据修订号、交接时间）与逐批冻结确认快照
  （裁决版本/结论与出货资格版本/状态，按确认清单顺序保序；`batch_no` 全局唯一，
  任一批次已在其他交接发运即整单拒绝；只追加，后续更正/待复核/清单修订不改写）

所有写操作在单事务（`BEGIN IMMEDIATE`）内完成并进程内串行化，配合外键约束与 WAL，
保证更正与登记的原子性：校验失败即整体回滚，不会产生半成品批次或半成品重算版本。
核验单创建（未知/重复批次在写入前整体校验）、重新核验、批次清单修订与确认
（校验与快照写入同一事务，失败不留下成员/快照/确认记录）遵循同样的原子性保证；
装车交接同样在单事务内完成逐批校验与全单批次占用，重复交接号/未知单据/状态或
版本冲突/任一不合格批次（含批次已在其他交接发运）均整体回滚，不留下部分占用，
交接表以 `CREATE TABLE IF NOT EXISTS` 幂等加入，旧数据库启动时自动建立；
新表以 `CREATE TABLE IF NOT EXISTS` 幂等加入，既有快照表新增列与
`verification.revision` 修订号列通过 `ALTER TABLE`（逐列检查）幂等补齐
（既有单据修订号按 1 计），旧数据库启动时自动升级，
并为既有批次补出货资格 v1（`UPGRADE_INIT`，不改变既有核验单状态；升级前的旧确认快照
出货资格字段为空，确认时要求先重新核验）；批次表的 `status`/`voided_reason`/`voided_at`
作废列同样幂等补齐（既有批次为 `ACTIVE`），作废相关索引在列迁移后建立；
既有批次裁决、更正与历史查询行为保持不变。
