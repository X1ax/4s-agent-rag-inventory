# 供应商与界面调整验收

日期：2026-10-23。本次为 M3.4 前业务调整，不代表 RAG 问答或 Agent 已实现；未提交 Git。

## 实现范围

供应商新增供货类别。空列表表示尚无供货资格，不表示全部备件。采购创建与确认均由后端复核供应商启用状态及备件类别；备件默认供应商也须符合范围。缩减供货范围前须调整受影响的默认供应商。

新建采购单移除顶部统一供应商，逐行先选供应商，再选其可供备件；未选供应商时禁用备件选择。更换或清除供应商同时清空本行备件和单价。不同明细可以使用不同供应商。采购及收货明细分别保存来源供应商，混合供应商单据的表头不强行归属其中一家。收货继承原采购明细，不因后来供货范围改变而改写已确认采购的归属。

迁移 `0005_suppliers` 已在专用开发库和测试库执行。历史采购与收货明细从原表头回填，原表头字段保留兼容；多供应商或已有供货范围时禁止破坏性回退。旧请求未提供明细供应商时维持原请求摘要，避免历史导入幂等失效。

`data/catalog/supplier-coverage.json` 为项目自建模拟范围，`backend.configure_suppliers` 显式配置已有三家模拟供应商及48种备件默认供应商，不在应用启动时运行：

| 供应商 | 可供备件数 | 默认供应备件数 | 图片 |
| --- | ---: | ---: | --- |
| 模拟供应商·保养件 | 9 | 9 | supplier-maintenance.png |
| 模拟供应商·油液 | 3 | 3 | supplier-fluids.png |
| 模拟供应商·综合件 | 45 | 36 | supplier-general.png |

保养件与综合件允许交叉供货。油液供应商仅包含合成机油、制动液和冷却液；综合件覆盖非油液备件。沿用三张不同场景图片，未新增供应商或图片。范围是类别级模拟资格，不代表真实供应商承诺或车型适配。

所有 Element Plus 下拉、分类级联框启用一键清除；必填项清空后须重新选择。初次调整时统计备件清除后移除旧结果；后续已改为清除后自动查询全部备件，更新统计不再要求指定备件，见 `statistics-overview-verification.md`。仓库清除恢复全部仓库。单选空值按接口语义明确设为 null 或空字符串。

当前需求仅作通用故障排查，移除“车型与适配”菜单及页面入口；保留已有后端、表与资料，不执行数据删除。全部侧栏菜单移除数字编号，标题及菜单文字居中。

## 实际验证

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_m0.py tests/test_m1.py tests/test_m2.py tests/test_history.py tests/test_suppliers.py tests/test_fitments.py -q
# 26 passed in 8.37s

# frontend 目录
& 'D:\DevTools\nodejs\node.exe' node_modules/vite/bin/vite.js build
# 构建成功；仍有既有 bundle 大于500 kB提示

.\.venv\Scripts\python.exe -X utf8 -m backend.configure_suppliers
.\.venv\Scripts\python.exe -X utf8 -m backend.seed_m2
.\.venv\Scripts\python.exe -X utf8 -m backend.seed_history
.\.venv\Scripts\python.exe -X utf8 -m backend.check_inventory
```

配置重复运行：48种备件、0项变更。M2 与60日历史重复导入成功。开发库243张单据、1102条业务明细、24条余额、912条库存流水及431条幂等记录，与变更前按主键排序计算的 SHA-256 完全一致（明细排除新增供应商列）；车型与适配记录仍均为0。历史供应商回填不一致计数0；48种备件默认供应商均在其供货范围内。只读对账的余额/流水、单据来源、调拨守恒均通过。

新增测试覆盖混合供应商采购、分批及混合收货、金额、重复请求、越范围拒绝及无部分写入、确认时重新校验、已确认采购收货来源、默认供应商约束和旧摘要兼容。真实业务写入测试仅使用隔离 MySQL 测试库。

浏览器验证：未选供应商时备件禁用；油液供应商仅显示3种油液，综合件显示45种非油液；两行可以选择不同供应商；清除第一行供应商后该行备件清空并禁用、单价从128.00恢复0.00，第二行保留原选择。关闭表单未创建开发库单据。统计仓库和备件的一键清除均通过。全部12个侧栏菜单的实际样式为居中，且无数字编号和车型适配入口。静态扫描所有 Vue 选择组件均包含 clearable。

截图：[采购明细](purchase-line-suppliers.png)、[侧栏居中](sidebar-centered.png)。尚未执行完整 RAG 回归或 M3.4；本次未更改知识源与索引。
