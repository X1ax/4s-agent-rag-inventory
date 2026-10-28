# M3.3 原生PDF与DOCX解析验收

日期：2026-10-23。已完成两份PDF、三份DOCX的原生解析并更新BGE-M3索引。本阶段未开发问答API、知识页面或Agent，未修改库存业务和.env，未提交Git。全部资料仍待核准，索引成功不代表制度已经生效。

## 原生解析与定位

新增`backend/app/knowledge_formats.py`，在`knowledge.py`按原生格式接入；传入已核对哈希的原生字节，不读取母稿作为运行时替代。MD/TXT沿用原分块与`controlled-text-v1`，新格式使用`controlled-office-v1`。原有155个单元的chunk_id和embedding_text与M3.2派生快照逐项比较，全部保持一致；解析快照ID加入原生解析版本，不覆盖之前的不可变导出。

| 原生资料 | 格式 | 新增单元 | 定位 |
| --- | --- | --- | --- |
| INV-PUR-003 | DOCX | 20 | 章节、条款及原生正文块 |
| INV-TRF-006 | DOCX | 18 | 同上，保留调拨时点表位置 |
| INV-SUP-012 | DOCX | 16 | 同上，参数表保留在C012与C013之间 |
| INV-RCV-004 | PDF | 18 | 实际第1—2页，表格位于第2页 |
| INV-WHS-007 | PDF | 15 | 实际第1—2页，表格位于第2页 |

新增87个单元后，共19份资料、242个知识单元（库存219、故障23）、86个章节上下文。来源报告未支持数与失败数均为0；全部19份只对显式草稿预览具备来源资格，默认现行范围仍为0。

DOCX用python-docx的`iter_inner_content()`遍历原有段落/表格顺序，核验标题、元数据表、版本、状态、起草日期及归口岗位。业务表格保留表头、行列、正文块和原文表格序号，不生成Word页码。未处理修订、合并/嵌套表格、深层或未支持正文结构进入需复核。

PDF用pypdf逐页读取文字层，只删除符合该登记资料模板的明确页眉/页脚，合并物理换行并保留条款页起止。元数据、全部条款ID及登记起始页均校验；跨页章节引用覆盖真实页范围。当前两份PDF没有单条款跨页，另以模拟提取层移动条款续行验证跨页拼接，不能将此测试冒称真实文件的跨页条款实例。

两份PDF使用已经目视核对的三列单行表格结构，按明确表头和行列数量重建；不猜测未知表格关系。PDF无文字层、加密、损坏、未知表头、换行/行列结构变化或页码不符时进入需复核。该解析器只支持已登记受控资料，不包含OCR、任意上传或通用复杂版面识别。

原始PDF第2页通过捆绑Poppler渲染并检查表格布局；渲染中间文件在忽略目录`data/chroma/rag/inspection/`。三份DOCX按原生XML块次序核对，没有改写或重新导出原始资料。测试另把原生提取的87条正文和5个表格与既有Markdown编制母稿做空白归一化的独立比对，全部一致；母稿只作测试参考，不参与运行时解析、来源或Embedding。

## 依赖与自动验证

项目依赖新增并锁定`pypdf==6.21.2`、`python-docx==1.2.0`，新增传递依赖`lxml==6.1.3`。沿用项目Python 3.13和原包源，未改变系统Python或包源。运行时依赖均在项目`.venv`，捆绑工具仅用于只读资料检查与PDF渲染。

```powershell
uv add --python 'D:\PyCharm_Projects\4S\.venv\Scripts\python.exe' pypdf python-docx
uv lock --check --python .venv/Scripts/python.exe
uv pip check --python .venv/Scripts/python.exe
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_knowledge.py tests/test_knowledge_formats.py tests/test_retrieval.py -q
.\.venv\Scripts\python.exe -X utf8 -m backend.parse_knowledge --check-only
.\.venv\Scripts\python.exe -X utf8 -m backend.parse_knowledge
```

依赖锁一致、124个已安装包兼容检查通过。相关测试95项通过、1项跳过，其中新增原生格式验证24项；跳过仍为M3.1的Windows符号链接权限限制。M3.2本地Chroma配置读取产生的弃用提示仍存在，未启用默认Embedding。没有前端或业务逻辑变更，未重复运行前端构建和MySQL业务测试。

验证包含原生87条全文、5个表格及原有次序；DOCX块定位、不伪造页码、未处理修订及合并表格阻断；PDF真实页码、跨页章节、模拟跨页条款、空白/加密/损坏、表头结构异常、页码登记错误；原生失败后不能使用母稿替代或导出可用快照。既有版本、哈希、独占锁、失败发布、重复复用和跨进程恢复测试继续通过。

当前解析导出为`data/chroma/rag/parsed/3d00858e586f10f4516a1abaa7a99971c018bd52d36e1c6cabbc12a4e26c71aa/`。首次导出`created=true`，再次导出`created=false`；旧M3.1快照保留。

## 真实索引与开发集检索

```powershell
.\.venv\Scripts\python.exe -X utf8 -m backend.build_knowledge
.\.venv\Scripts\python.exe -X utf8 -m backend.build_knowledge
.\.venv\Scripts\python.exe -X utf8 -m evals.run_dense_development
.\.venv\Scripts\python.exe -X utf8 -m evals.inventory.check_dataset
```

新活动代次`f80bb2fc5c644ffe834d21e0b5b9f355`，指纹`cc50f08795c2e85459d94bde96d7ea95f75859fffcf4f950863c812c74ee20a8`。普通版`BAAI/bge-m3`生成242条1024维向量，cosine距离；实际16次HTTP请求、0重试，上游报告39384个total_tokens，本次本机构建计时7.826秒，不作生产性能承诺。仍按小规模完整代次重建，没有提前增加增量向量缓存。第二次构建复用同代次，0次Embedding。

构建进程退出后，另一Python进程打开持久化索引运行20道development问题。新增`evals/run_dense_development.py`可重复运行当前草稿基线，结果新建独立文件，不覆盖已有结果或冻结输入。默认现行范围先验证为`no_eligible_documents`且0次Embedding；再显式查询草稿。未调用对话模型、未执行验收集、未生成故障结论。

本次实际记录：[完整开发集向量检索结果](../evals/runs/dense-development-20261023T043038125912Z.json)。文件保留输入哈希、代次指纹、查询、真实排序、距离、格式定位、父上下文以及预期证据组命中情况。

| 知识域 | 开发题 | top-5预期证据组命中 | 完整覆盖问题 | 未运行 |
| --- | --- | --- | --- | --- |
| 库存制度 | 12 | 17/21 | 10/12 | 0 |
| 通用故障 | 8 | 9/9个症状证据组 | 8/8 | 0 |

20次真实查询，上游报告526个total_tokens、0重试。故障公共规则作为必要上下文补充，不给症状召回加分；这些数据不是回答准确率或维修正确率。库存DEV-INV-01和DEV-INV-08仍未覆盖指定证据；没有修改问题或调参掩盖失败。与M3.2相比，本轮库存可检索资料从7份扩大到12份、问题从7题扩大到12题，不能把两个分母下的数字直接声称为性能提升。

此前依赖原生文件的5题全部命中预期证据：DEV-INV-03命中DOCX正文块20，DEV-INV-04命中PDF第2页C014，DEV-INV-06命中DOCX正文块13，DEV-INV-07命中PDF第2页C009/C010，DEV-INV-12命中DOCX正文块10—12的C004/C005/C006。范围仍为待核准草稿，不代表实际业务或供应商承诺。

原有库存评测的48题资料/哈希/编号与6项确定性数据检查继续通过；校验器的`model_evaluations_run=0`表示该校验器没有运行模型，实际Embedding检索另见上述运行记录。冻结development/acceptance文件及原生来源清单均未改写。

## 下一步

进入M3.4：按来源处理修改、删除、撤回后的部分可用状态；在保持模型、语料、k及问题集一致的条件下加入编码精确匹配和关键词融合，对照本次纯向量基线。当前只保守阻止来源变化后的整个旧代次，尚无这套精细生命周期与组合检索。引用问答/API/页面留M3.5，完整回答与Agent验收尚未执行。
