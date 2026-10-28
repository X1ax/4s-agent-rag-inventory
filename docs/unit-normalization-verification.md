# 库存单位统一为件：变更与验收

日期：2026-10-23。依据用户明确要求，库存统一为“件”，特殊备件通过包装规格说明区分；未提交 Git。

## 口径与数据

一件指一个SKU的完整库存包装，普通单品、整瓶、整桶、整套均按整数件收发。件数可以汇总为库存包装数量，不代表液体总体积、套内零件总数或车型维修所需数量。同一件数不代表不同规格相同或可以互换；容量及套内数量未知时保留待核准。

现有48种备件中，6种原单位为桶、瓶或套：DEMO-004至008、CAT-032。显式执行 `backend.normalize_units`：保留名称，补充整桶/整瓶/整套按1件计数且不拆零的规格说明，清除受影响的身份/适配核准，并记录审计。数量、单价、安全库存、历史单据及流水不换算。原标识、规格、核准字段已先保存于被Git忽略的 `data/local-backups/units-20261023T081427403466Z.json`。重复执行返回0项变更。

新增/编辑备件页面不再提供单位选择，固定显示“件（一个完整库存包装）”；后端 PartInput 仅接受件，省略单位时默认件，其他单位返回422。新模拟数据与36种扩展目录同样统一为件；原SKU与计数数量不变。历史导入的单位核对继续生效。

页面只有一套统计，全部仓库、全部备件可直接查询。当前现有198件、采购待收12件、调拨在途5件；包装说明可在备件详情查看。后续用户明确要求趋势也统一按件，现已改为按业务日期汇总实际采购入库与已过账领用件数；排行仍按领用频次并附件数。领用频次不充当补货数量计算输入。详见 `trend-quantity-verification.md`。

## 知识与评测一致性

总则INV-POL-001、主数据INV-MDM-002、统计INV-ANA-009修订为1.0-draft.2，登记新哈希，仍为待核准稿。整瓶/整桶/整套由库存单位改为包装规格；受控历史标签归一、包装件数汇总及单据频次边界均有条款。未修改其他PDF/DOCX原生文档。

评测版本同步为1.0-draft.2：DEV-INV-02与ACC-INV-21修订单位相关情境，其余题仅更新受影响文档引用版本（ACC-INV-31同时更新现版本说明），12开发题、36验收题不增减。旧评测、来源锁、清单和三份旧原文归档于 `evals/inventory/archive/1.0-draft.1`，历史成绩不作为新版成绩。本次改题由业务规则变更触发，不是根据检索结果调整验收题。

19份资料重新解析为242个单元、86个上下文；真实BAAI/bge-m3索引重建成功：1024维、16次请求、242条文本，无重试。新代次 `e1361063d66e44b4aafc2dc87ba307c2`。使用开发题的真实检索首条命中INV-MDM-002-C005，版本1.0-draft.2；记录见 `unit-policy-retrieval.json`。未运行验收题模型问答，不声称回答准确率或M3整体完成。

## 实际验证

```powershell
.\.venv\Scripts\python.exe -m alembic -x database=test upgrade head
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_units.py tests/test_statistics.py tests/test_m1.py tests/test_m2.py tests/test_suppliers.py tests/test_fitments.py tests/test_history.py -q --tb=short
# 24 passed in 8.52s
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_knowledge.py tests/test_retrieval.py -q --tb=short
# 71 passed, 1 skipped in 21.45s；Windows符号链接权限限制导致跳过
.\.venv\Scripts\python.exe -X utf8 evals/inventory/check_dataset.py
# 48题输入、引用版本及来源锁通过；模型评测0次
.\.venv\Scripts\python.exe -X utf8 -m backend.parse_knowledge
.\.venv\Scripts\python.exe -X utf8 -m backend.build_knowledge
.\.venv\Scripts\python.exe -X utf8 -m backend.normalize_units
# 再次执行：0项变更
.\.venv\Scripts\python.exe -X utf8 -m backend.seed_m2
.\.venv\Scripts\python.exe -X utf8 -m backend.seed_history
.\.venv\Scripts\python.exe -X utf8 -m backend.check_inventory
# 余额/流水、单据来源、调拨守恒均通过
```

前端Vite构建成功；既有bundle大于500 kB提示仍存在。测试覆盖旧整桶标签归一后数量、价格、阈值与流水保持不变，重复归一不重复审计，新增件数默认值与其他单位拒绝，统计全仓/单仓/单品、部分收货、在途、同单去重与空日期区间。适配迁移拒绝回退测试增加finally恢复head，避免较新迁移已经回退后污染后续测试。

开发库243张单据、1102条明细（排除此前新增供应商字段）、24条余额、912条流水、431条幂等记录，与变更前按主键排序的SHA-256完全一致。车型与适配仍为0记录。备件单位与包装说明为本次预期元数据变更。所有业务写入测试位于隔离测试库；浏览器核对只查询或打开表单，未新增开发库单据。

截图：`unit-unified-statistics.png`。原多单位方案记录见 `statistics-overview-verification.md`，最终以本文件为准。
