# 模拟数据初始化与历史补齐

这些命令是本项目已有初始化脚本的操作入口，不是数据库导出。仅对已配置的项目专用开发库执行；已有业务库先备份，不在应用启动时重跑。新环境完整复现尚未验收，具体约束以脚本校验为准。

## 基础数据

在安装依赖、填写 `.env` 并执行 `alembic upgrade head` 后：

```powershell
.\.venv\Scripts\python.exe -m backend.seed_demo
.\.venv\Scripts\python.exe -m backend.expand_catalog
.\.venv\Scripts\python.exe -m backend.configure_suppliers
```

初始为 12 类备件、两仓和三家模拟供应商，扩展后共 48 类通用备件。新增目录本身不等于已产生库存或真实厂家适配。`seed_demo` 缺少登录配置时生成随机值保存到本机 `.env`，不会把密码写入仓库文档。

## 可选业务事件与历史

```powershell
.\.venv\Scripts\python.exe -m backend.seed_m2
.\.venv\Scripts\python.exe -m backend.seed_history
.\.venv\Scripts\python.exe -m backend.configure_reference_prices
.\.venv\Scripts\python.exe -m backend.seed_catalog_history
.\.venv\Scripts\python.exe -m backend.configure_safety_stock
```

历史从期初、采购、入库、领用和调拨事件生成，并使用业务服务推导余额；固定种子、日期区间和请求标识用于复现与防重。原 12 类与新增 36 类采用不同历史计划，不把模拟覆盖当作真实需求分布。参考价属于旧历史生成输入，不等于当前供应商供货报价。

## 供货价、门店价与成本补齐

既有模拟历史缺少价格/成本信息时，先生成计划，再检查计划中的数量、金额、范围及缺失项，确认后显式执行：

```powershell
.\.venv\Scripts\python.exe -m backend.backfill_pricing --plan data/local-backups/pricing-plan.json
# 阅读本机生成的计划后再执行下一行
.\.venv\Scripts\python.exe -m backend.backfill_pricing --apply data/local-backups/pricing-plan.json
```

不要覆盖已有同名计划，不手工修改其摘要以绕过快照检查。计划、备份与本地数据库不提交 Git。完成后运行：

```powershell
.\.venv\Scripts\python.exe -m backend.check_inventory
```

历史来源、价格与已验证边界见 [目录历史](catalog-history-statistics-verification.md)、[价格与毛利](pricing-gross-profit-verification.md)、[操作说明](operation-guide.md)。账号密码、数据库连接与 API Key 均需在本机独立配置。
