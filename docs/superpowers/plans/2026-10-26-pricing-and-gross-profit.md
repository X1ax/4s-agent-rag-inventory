# 双价格与实际成本毛利 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 实现默认供货价与门店定价、收费/质保出库、实际移动加权成本毛利及可审计历史补录。

**Architecture:** 复用现有业务事务和库存行锁，余额保存总价值，流水保存价值变动，出库保存结算和成本快照。报价、售价和只读毛利查询独立接口，前端与Agent共用服务端规则。

**Tech Stack:** 现有Python/FastAPI/SQLAlchemy/MySQL、Vue3/Element Plus，不新增依赖。

**Spec:** `docs/superpowers/specs/2026-10-26-pricing-and-gross-profit-design.md`（用户已确认，并确认默认报价展示、其他报价展开）。

## Global Constraints

- 不提交Git、不清空既有数据、不改变原数量流水和历史采购入库成交价。
- 固定种子20261026，模拟补录明确来源；成本Decimal至少40位，分ROUND_HALF_UP。
- 权限来自认证：管理员供货价，管理员/经理门店定价；库管不能改价；Agent没有改价工具。
- 新出库明确paid_repair/warranty；质保0结算且有说明，收费必须有非零门店定价。
- 价格、成本、单据、库存、审计与幂等结果共用事务。数量和价值含在途都守恒。

## Review Focus

- 回填业务日期不得改变成本实际执行顺序；测试按流水ID重放。
- 0元合法供货与缺失报价有区别；测试0价来源说明和缺价拒绝。
- 多行中一行成本不完整/库存不足不得部分过账；测试回滚全部价值与数量。
- 历史补录重跑或基线变化不能覆盖人工修改；测试指纹冲突和幂等。
- 收费/质保混合区间毛利率不能平均比率、质保不能重复扣减；测试聚合与缺失计数。

## 统一数据与接口契约

由主执行者修改models/schemas/migrations/documents/inventory/agent及公共测试fixture，其他任务不得改这些文件。

模型新增：
- `SupplierPrice`: id, supplier_id, part_id(unique pair), supply_price Decimal(12,2), version int, active bool, source str(300), updated_at。
- `PriceChange`: id, part_id, supplier_id nullable, kind str(supply/sale), before_price nullable, after_price Decimal(12,2), source str(300), operator_id, created_at。
- `PricingBackfill`: version str primary key, plan_digest str64, quantity_fingerprint str64, summary JSON, created_at。
- `Part.sale_price` nullable Decimal12,2、`sale_price_version` int默认0。
- `OpeningLine.unit_cost` nullableDecimal12,2、`cost_source` str300。
- `StockBalance.stock_value` nullableDecimal24,2；`StockLedger.value_delta` nullableDecimal24,2。
- `Document.issue_type` nullablestr20、`warranty_reason` str200、`work_order` str80、`pricing_source` str100。
- `DocumentLine.sale_unit_price` nullableDecimal12,2、`cost_amount` nullableDecimal24,2、`quote_version` nullableint、`sale_price_version` nullableint、`pricing_source` str100。

接口（金额均返回字符串）：
- GET `/api/pricing/parts?part_id=&supplier_id=` → `{items:[{part_id,sku,name,default_supplier_id,sale_price,sale_price_version,suppliers:[{id,name,supply_price,version,active,source}]}]}`。只显示当前供货资格范围报价，包含无报价合格供应商以识别缺价。
- PUT `/api/pricing/suppliers/{supplier_id}/parts/{part_id}` body `{supply_price,source,active,expected_version}`，admin；missing initial version0。
- PATCH `/api/pricing/parts/{part_id}/sale-price` body `{sale_price,source,expected_version}`，admin/manager。
- GET `/api/profits?warehouse_id=&part_id=&category=&date_from=&date_to=&sort_by=margin|margin_rate|quantity&page=&page_size=7` → `{date_from,date_to,totals:{revenue,paid_cost,gross_profit,gross_margin_rate,warranty_cost,contribution,paid_quantity,warranty_quantity,missing_lines,missing_quantity},ranking:[{part_id,sku,name,unit,revenue,paid_cost,gross_profit,gross_margin_rate,warranty_cost,paid_quantity,warranty_quantity}],ranking_total,trend:[{business_date,revenue,paid_cost,gross_profit,warranty_cost}],details:[{document_id,business_date,warehouse,name,sku,issue_type,quantity,sale_unit_price,revenue,cost_amount,gross_profit,pricing_source}],details_total,page,page_size,notice}`。details同页分页（排行和明细页码可分离扩展为details_page默认page），数据缺失必须可见。
- 新DocumentInput增加issue_type/warranty_reason/work_order；DocumentItem允许quote_version，单价由服务端对照报价，sale_unit_price/cost_amount不可由客户指定。详情返回新增快照，仍保留旧unit_price。

## Task 1: 迁移与共享成本事务（主执行者）

**Files:** models.py, schemas.py, inventory.py, documents.py, `backend/app/costing.py`, `0008_pricing_costs.py`, tests/test_costing.py, tests/test_pricing_workflow.py，测试公共fixture。

**Interfaces:** `money(value) -> Decimal`、`allocated_cost(value,quantity,out_quantity)->Decimal` in costing；documents在change_stock中保存value_delta和line.cost_amount。

- [x] 写失败测试：先入10件100元/件再入10件120元/件，出3件成本330、剩余1870；质保0结算仍扣成本；全数出库无尾差；调拨接收固定发送成本。
- [x] 运行pytest观察失败，再添加迁移/最少实现；两库升级0008，不在迁移中生成模拟数据。
- [x] 校验伪造价、缺用途、未知成本、多人重复/并发及失败回滚；旧测试fixture明确补报价/期初成本/出库用途而不降低生产校验。

## Task 2: 报价、售价和毛利只读接口（独立后端任务）

**Files:** `backend/app/pricing_api.py`, `backend/app/profits.py`, tests/test_pricing_api.py；主执行者负责在main.py注册router。

**Interfaces:** 按上述模型和HTTP契约；读共享models，不能改documents或schemas。价格修改行锁、expected_version冲突409、记录PriceChange及AuditLog。

- [x] 先写失败测试：报价按供应商独立、库管/经理禁止供货修改、经理只可改售价；0供货价必须说明，缺报价不可伪装0。
- [x] 实现查询和独立价格维护，新增0售价拒绝。
- [x] 写并验证收费/质保利润、过滤、分页、负毛利、0收入率、未知成本统计；交易行与流水issue日期一对一关联，不能乘多重join。

## Task 3: 页面（独立前端任务）

**Files:** CatalogPanel.vue, DocumentsPanel.vue, `ProfitPanel.vue`, StatisticsPanel.vue或App.vue（仅接入分析区），前端金额工具测试按需要。

**Interfaces:** 上述HTTP契约；目录API的sale_price/version为只读派生，普通资料保存时删除它们。资料默认显示默认供应商供货价及门店定价，其他报价展开；独立弹窗维护价格，权限不影响其他资料。

- [x] 价格有关计算先写Node测试，再实现；不为静态样式增加镜像测试。
- [x] 采购行去除任意改价、只可选择有报价备件；出库选择用途且质保说明；详情展示结算和成本快照。
- [x] 毛利四指标、7条分页排行、时间趋势、收费/质保占比、追溯明细与持续错误；保留全部筛选/清除。
- [x] npm构建，后端联通后由主执行者浏览器核验三角色。

## Task 4: 可复现历史补录（独立脚本任务）

**Files:** `backend/backfill_pricing.py`, tests/test_pricing_backfill.py。

**Interfaces:** `make_plan(session)->dict`，`apply_plan(session,plan,operator_id)->dict`；CLI `--plan <path>`生成计划，`--apply <path>`核验并执行。使用统一models和costing算法，不能改其他业务模块；仅允许fours_agent_dev/fours_dev，测试直接函数用隔离测试库。

- [x] 先写失败测试：旧采购/入库价格保留、报价默认/备用差异及售价与批准候选表一致、期初成本来源、两类用途和SKU月份固定价、流水ID重放、余额一致及在途固定成本。
- [x] 计划包含目标新字段前后值、种子、digest、原数量/历史采购价指纹，备份原值。执行用稳定仓库/备件锁、同事务；已完成版本重复执行只返回原结果；首次执行基线变更拒绝。
- [x] 首次只填缺失值，人工价不覆盖；失效旧pending提案；报告各类数目及成本守恒。脚本不能由应用启动触发。

## Task 5: Agent、资料与整体验证（主执行者）

**Files:** agent_tools.py, agent_proposals.py, agent.py相关提示，business_api.py详情，README及受影响知识资料与验证记录。

- [x] 工具返回供应商报价、门店定价和只读毛利，schema明确收费/质保；提案快照包含报价/售价版本、用途、成本输入，未知用途追问，伪造价拒绝，保留集中确认。
- [x] 有意义的MySQL集成和全pytest、前端Node测试与构建；实际浏览器两类出库及统计、三角色价格权限。
- [x] 后端停止后备份并显式补录开发历史，重复运行不变；数量基线和价值对账；再启动服务验证持久化。
- [x] 更新资料与Agent约束，若改知识语料则受控索引更新；记录真实模型验证边界。新鲜独立审查及定向修复，未提交Git。

## 执行记录

用户已明确要求按批准方案实施，无需再次确认执行方式。Ruling：在当前未提交工程原地工作，Git几乎全部untracked，创建worktree不能包含当前系统；不提交、不拷贝凭据到别处。主执行者统一共享字段与事务，三个独立文件域按dispatching-parallel-agents并行，测试库仅用唯一前缀fixture；后端迁移与历史写入串行。


收尾记录（2026-10-26）：上述交付与运行步骤已执行；勾选表示步骤完成，不表示完整回归全绿。最终财务定向27项与前端7项通过、构建成功；完整后端285通过/1失败/1跳过，既有Chroma间歇失败复测20项通过但未根治。开发库已补录并验证幂等与数量/价值对账，3个真实模型开发案例通过。实际命令、首次失败、复测、三角色界面与新语料版本见docs/pricing-gross-profit-verification.md；不扩大M3/M4或独立评测结论，未提交Git。
