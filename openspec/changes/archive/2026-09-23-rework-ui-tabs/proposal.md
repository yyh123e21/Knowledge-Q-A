# Proposal

## Why

应用里有两套入库入口、两套查看切分的入口、一页与知识库无关的学习笔记，以及一页藏着一张
文档表的「学习统计」。使用者要的是：入库只在切分工作台做，知识库里有什么、怎么删，在一页
里看全。

两套入口的代价是实测出来的：`load_document`（`11_Q&A_Assistant.py:405-424`）把
`_register_document`（同文件 `262-280`）的五行赋值与同一条 `memory_tool.run` 抄了一遍，
两份代码只差注释；`commit_preview` 的返回值与 `refresh_doc_choices`（`1318`）各自拼一次
文档下拉框，正是 `openspec/config.yaml` 架构表里 `document_choices()` 那一行警告的
「两条入库路径拼出两种形状的文档下拉框」。删掉其中一条路径，这类分叉就少一处来源。

## What Changes

1. **「🏠 开始使用」删掉「加载文档」与「🔍 查看文档切分」两段** ——
   `11_Q&A_Assistant.py:1280-1322`，含 `file_upload`、`load_btn`、`load_output`、
   `doc_selector`、`viz_btn`、`text_btn`、`chunks_output` 与 `refresh_doc_choices`。
   该 tab 只剩「用户ID + 初始化助手」。
   **BREAKING**：应用里不再存在不经过预览的入库路径。现有两处已经禁止它——
   `commit_preview` 的 `is_fresh` 门（`11_Q&A_Assistant.py:350`）与
   `ChunkingWorkbench.commit` 的 `ChunkingError("尚未预览，无法入库")`
   （`chunking.py:1737`）——所以删掉一键加载之后，入库严格是「预览 → 确认入库」两步。
2. **删掉一键加载的实现**：`PDFLearningAssistant.load_document()`（`378-454`），以及随之
   失去唯一调用方的 `_index_with_strategy()`（`237-260`，全项目只在 `398` 被调用一次）。
3. **删掉回看**：`get_chunks_visualization()`（`542-630`）、`get_chunks_info()`
   （`653-720`）、`document_choices()`（`951-965`）、UI 回调 `view_chunks_ui`（`1157`）
   与 `get_chunks_text_ui`（`1170`）。
   `_get_chunks_from_qdrant()`（`455`）**保留**：`_stored_documents()`（`938`）靠它给
   知识库文档清单逐份量质量，删掉回看不会让它变成死代码。
4. **删掉「📝 学习笔记」整页**：tab（`1462-1481`）、`notes` / `notes_file`（`154-155`）、
   `_load_notes`（`160`）、`_save_notes`（`183`）、`add_note`（`817`）、`get_notes`
   （`848`）、`add_note_ui`（`1137`）、`view_notes_ui`（`1148`），以及页首说明里
   「📝 学习笔记记录和学习历程回顾」那一行（`1264`）。
5. **「📊 学习统计」整页换成「📚 知识库文档」**（`1483-1492`）：
   - 「刷新列表」→ 渲染现有的 `documents_table()`（`967`，数据来自 `ck.list_documents`）；
   - 文档选择器 + 确认勾选 + 「删除」按钮 → 新方法 `delete_document(source_path)`，
     调 `ck.purge_document`（`chunking.py:1644`）后**回读**
     `ck.count_document_chunks(...) == 0`，回读不通过就报失败。删除的定义是使用者给的：
     **不需要重启应用，但那份文档的内容搜索不到了**——删完立即生效，同一会话里再检索就取不到
     它（依据见 design.md D11：检索实时查 Qdrant，进程内没有文档索引与检索缓存，
     `delete_vectors` 带 `wait=True`）；
   - `documents_table()` 增一列「切分策略」，取值来自 chunk payload 里已有的
     `split_strategy`（读法见 `_doc_split_strategy`，`632`）；
   - 删掉 `get_stats`（`881`）、`get_stats_ui`（`1187`）、`generate_report`（`999`）、
     `generate_report_ui`（`1236`）。根目录不再落 `learning_report_*.json`。
6. **必须改到切分工作台 tab 内的两处**（结构、控件、交互、预览逻辑一律不动）：
   - `1330` 那句「只想快点用的话，「🏠 开始使用」里的一键加载走的是默认策略。」——
     它宣传的入口没有了，留着就是界面对使用者说假话；
   - `1378-1380` `wb_commit_btn.click(...)` 的 `outputs` 去掉已不存在的 `doc_selector`。
7. **删掉随之失去读者的会话状态**：`self.stats`（`140-145`）、`loaded_documents` /
   `document_file_map`（`150-151`）、`current_document` / `current_file_path`
   （`148-149`）、`_register_document()`（`262-280`），以及 `ask()` 里的
   `stats["questions_asked"] += 1`（`764`）。删除后这五项在本文件里都没有读者了。
8. **`ask()` 去掉 `current_document` 前置校验**（`732-733`）：知识库是持久的，重启后库里
   有 390 块却因为「本次会话没入库过」而拦下提问，是上个变更 D13 修过的同一类毛病
   （会话态的门卡住持久态的资源）。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `document-chunking`：删掉「保留默认快速路径」这条入库路径及其验收；删掉回看相关场景
  （覆盖率与切分来源的观察面从回看移到知识库文档清单）；新增「删除已入库文档」；
  「提取质量在入库前可见」换名重写为「提取质量在预览时可见」（见下）。
- `rag-namespace-consistency`：「读路径与写入同命名空间」正文里列的读操作清单随本次变化
  ——「回看」换成「删除」，「统计」随学习报告一起消失；对应的那条 scenario 由回看改为删除
  （删除是比回看危险得多的跨命名空间操作，`_document_point_ids` 的过滤条件就是为它写的）。

两处 scenario 的功能面随本次消失——`快速路径给出同一告警`（一键加载）与
`回看只呈现本命名空间的内容`（回看）。校验与归档都不允许 MODIFIED 丢掉 scenario，也不允许
同一份 delta 里 REMOVED 与 ADDED 同名，所以这两条 requirement 只能整条换名重写：

- `提取质量在入库前可见` → `提取质量在预览时可见`（正文与其余四条 scenario 逐字保留）
- `读路径与写入同命名空间` → `读写与删除同命名空间`（正文与两条 scenario 逐字保留）

两条约束本身都**没有**被删除，只是名字与读操作清单跟着界面变。理由与代价见 design.md D10。

## Impact

- `11_Q&A_Assistant.py`：删除约 500 行（第 1–5、7、8 条列出的方法、tab 与状态），
  新增 `delete_document()` 与新 tab 的接线。
- `chunking.py`：**不改**。`purge_document`、`count_document_chunks`、`list_documents`
  三个原语已经够用，且 `_document_point_ids` 已按 `source_path` + `rag_namespace` 双条件
  过滤（`1488-1520`）。
- `chunk_viz.py`：`render_chunk_report` 只剩预览一个调用方；`quality_label`（`264`）失去
  原调用方 `document_choices`，转给新页的文档选择器用作标签前缀。
- 知识库 `docs_yyh`（封思敏 327 块 + 朱光熙 63 块）：本次只读核对，不迁移、不重切；
  新增的删除功能只由使用者在界面上显式触发。
- 磁盘上既有的 `notes_*.json` 成为孤儿数据，本次不删（见 Non-goals）。

## Non-goals

- **不重排、不改动「🔪 切分工作台」的控件、交互与预览逻辑**。上面第 6 条列的两处是删掉
  `doc_selector` 与一键加载之后的必然改动，不是重设计。
- **不删除磁盘上已有的 `notes_*.json`**：它们落在章节目录里，属于运行期数据而非本次要动的
  代码；留着也便于回退。
- **不迁移、不重切、不删除知识库里的既有 chunk**（含早期章节脚本写进 `default` 命名空间的
  那两份）：本次不做存量数据治理。
- **不修 `chat()` 对 `recall()` 的调用**（`1104`）：`recall` 在本文件里根本没有定义，
  点「我之前学过什么？」会报错。这是既有缺陷，与本次三个请求无关，另开变更。
- **不清理 `memory_tool` 的写入**：`ask()`（`736`、`755`）仍在写工作/情景记忆，而本次之后
  这些写入在本文件里没有任何读者（`recall` 无定义、`generate_report` 被删）。清不清理都
  不改变可观察行为，属于另一个变更。
- **不重构 `ask()` 的检索参数**（MQE/HyDE、`limit=5`）。
- **不动 `01_*.py` … `10_*.py` 与 `*备份.py`**。
