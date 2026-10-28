# 入库与领用趋势恢复件数口径

2026-10-23，按用户最新要求，仅将趋势图改为实际数量，单位统一为件。复用接口已有 `trend` 数量序列，领用负向流水在图中取绝对值显示；不再读取 `activity_trend` 单据次数。图例、无障碍标签及说明同步更新，期初和调拨不计入图中两条曲线。仓库、备件和日期筛选继续有效；领用频次排行不在本次修改范围。

验证命令：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_statistics.py tests/test_m2.py -q --tb=short
# 7 passed in 1.95s
# frontend目录
& 'D:\DevTools\nodejs\node.exe' node_modules/vite/bin/vite.js build
# 构建成功，既有大于500 kB提示保留
.\.venv\Scripts\python.exe -X utf8 evals/inventory/check_dataset.py
.\.venv\Scripts\python.exe -X utf8 -m backend.parse_knowledge
.\.venv\Scripts\python.exe -X utf8 -m backend.build_knowledge
```

新增数量断言覆盖同单5个备件实际入库5件（不是1张）、两次领用累计6件（不是2张）、单仓收货5件、单品领用2件及无收发日期区间。没有业务数据写入开发库。

库存统计制度INV-ANA-009升级1.0-draft.3，仅修订趋势口径；来源哈希、评测引用版本同步，问题与答案要点未改。旧1.0-draft.2来源与评测归档。输入检查48题通过，19份资料242单元/86上下文重新解析并以真实BGE-M3重建1024维索引，活动代次171b49756d114787934da603c0831743。未执行模型问答评测。

浏览器显示“数量（件）”和实际数量曲线，仍只有一个趋势图。截图见 `trend-quantity.png`。未提交Git。
