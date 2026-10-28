# M3.1 文本知识解析验收

完成日期：2026-10-23。范围为登记的 Markdown/TXT 原生资料解析、稳定知识单元、章节上下文、来源资格与可复现导出。没有调用模型、生成向量、建立检索接口或修改业务数据库。

## 实际交付

新增 backend/app/knowledge.py，使用 Python 标准库读取两套受控来源清单，核对原生路径、SHA256、UTF-8、平铺元数据头、文档身份、版本、来源状态及预期条款/章节。源清单及正文均未回写，现有库存问答来源锁继续有效。

14份支持资料全部解析成功：库存制度7份（6份Markdown、1份TXT），形成132个完整条款；故障手册7份Markdown，形成15个完整症状条目和8条公共规则。合计155个知识单元、60个章节父上下文。

另5份原生文件（2份PDF、3份DOCX）只核对路径和哈希，状态为 unsupported_format，错误标识 format_not_supported_in_m3_1；没有把它们的Markdown母稿导入来代替原生解析。全部19份的登记及处理状态写入报告。

每个单元保存知识域、来源ID/版本/SHA256、稳定chunk_id、unit_id、parent_id、标题/章节、正文、嵌入候选文本及其哈希、原生行号、来源类型与状态、解析/分块版本。Markdown/TXT没有伪造页码。章节上下文保留文档说明、同章节条款和表格；故障条目保留五类必要条件。公共规则的换件状态表保存完整表头和四行对应关系。

默认 released_only 范围为0份，显式 draft_preview 的来源资格为14份。此资格仅表示来源可在指定范围内使用，不表示已经索引或可以检索；报告与全部单元仍为 not_indexed，retrievable 为false。资格判定日期写入快照，不作为永远有效的批准结论；未来服务仍须实时复核。

## 实际运行

在项目根目录、独立虚拟环境执行：

~~~powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_knowledge.py -q
.\.venv\Scripts\python.exe -X utf8 -m backend.parse_knowledge --check-only
.\.venv\Scripts\python.exe -X utf8 -m backend.parse_knowledge
.\.venv\Scripts\python.exe -X utf8 -m backend.parse_knowledge
~~~

新增解析CLI只导出派生文件，不创建活动索引指针。两次导出使用相同parse_id：212bd54cf1e070db0762f407b53f9c55a03ae184da2c8a0ddfcd9c4b2f6175a0；第一次 created 为true，第二次为false，已有快照不被覆盖。

本机派生文件位于：

~~~text
data/chroma/rag/parsed/212bd54cf1e070db0762f407b53f9c55a03ae184da2c8a0ddfcd9c4b2f6175a0/
  report.json
  units.jsonl
  contexts.jsonl
~~~

该目录沿用已有 data/chroma 忽略规则，不提交Git。parse_id还包含来源资格核验日期；日期变化可产生新的解析快照，但来源及规则不变时chunk_id保持稳定。未来索引构建须按来源和Embedding指纹判断是否需要重嵌入，不能把日期变化当作必须重复调用Embedding的依据。

另通过项目解释器的 -I -S 模式（不加载site-packages），显式禁用socket.create_connection后成功解析155个单元；进程未导入chromadb、httpx、sqlalchemy、pydantic或dotenv。证明此解析步骤不依赖模型、数据库、第三方解析包或.env配置，不是Mock模型测试。

## 测试与修正

最终36项通过、1项跳过。跳过项为真实符号链接越界测试，本机Windows不允许当前进程创建符号链接；绝对路径、相对路径越界、非原生目录及导出路径边界仍有实际测试。未为该测试更改Windows权限或系统设置。

测试覆盖：真实资料数量与引用、稳定ID及解析版本变化、元数据不一致、重复JSON字段、缺失/重复/额外/乱序条款、未知或超长结构、无效UTF-8、表格行列错误、完整故障限制、公共规则失效导致故障域被阻断、来源/辅助映射在解析期间或之后变化、草稿不能靠布尔标记升格、批准记录/生效期限制、只读指令数据、失败退出与不可变导出。

首次测试发现Windows文本写入默认换行转换导致第二次导出误报快照冲突，已将派生文件写入统一为UTF-8/LF，重复导出与损坏快照拒绝覆盖测试均通过。没有修改原生资料换行或其哈希。

## 当前限制与下一步

当前解析器支持已登记资料的受控格式：一个标题、二级Markdown章节或TXT编号章节、平铺标量元数据头和普通Markdown表格。更深标题、代码围栏、超长单元或无法确认的表格结构进入需复核状态，不宣称已实现通用Markdown/YAML解析。任一来源失败或需复核时，CLI返回非零且不导出可用快照；可核查具体文档错误，修复后重试。

尚无正式Embedding、Chroma集合、索引代次切换、关键词/向量检索、问答API或页面，也未执行48题库存或30题故障的真实模型评测。本阶段不需要停用正在运行的业务服务；后续M3.2的Chroma构建才实施单进程所有者锁和停服务维护边界。

下一步为M3.2：基于这些已校验单元完成显式在线Embedding、隔离本地索引、向量检索基线和跨进程恢复。PDF/DOCX原生解析留在M3.3。本次未修改依赖、现有业务路由、前端或.env，未连接库存数据库、未写库存或提交Git。

