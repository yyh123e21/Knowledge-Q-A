# Design

## Context

动机见 `proposal.md` — Why。这里只记影响方案的现状：

- 已入库内容目前只有一个观察面：「🏠 开始使用」里的「🔍 查看文档切分」
  （`11_Q&A_Assistant.py:1291-1322` → `get_chunks_visualization` / `get_chunks_info`）。
  它读 Qdrant 里已存的 chunk，从不经过 `extract_markdown`，所以**源文件已经不存在的文档
  也看得到**——而库里多数文档正是从 Gradio 临时路径入库的。
- 文档清单已经存在：`documents_table()`（`967-997`）渲染 7 列表格，数据来自
  `_stored_documents()`（`917-949`）→ `ck.list_documents`（`chunking.py:1528`）。
  本次要加的删除动作，下层原语 `ck.purge_document`（`chunking.py:1644`）与
  `ck.count_document_chunks`（`chunking.py:1523`）都已经在，且按
  `source_path` + `rag_namespace` 双条件过滤（`chunking.py:1488-1502`）。
- 问答页的门槛（`732-733`）以 `current_document` 为条件，那是会话态；知识库是持久态。
- 知识库是**单集合 + `rag_namespace` 载荷标签**，全部身份共用一个集合，删除是跨身份
  可见的破坏性操作。
- 本目录**不是 git 仓库**，没有版本控制回退。

## Goals / Non-Goals

**Goals:**

- 「🏠 开始使用」只剩初始化；入库只有「🔪 切分工作台」这一条路径。
- 已入库内容的管理（看清单、看切分来源、删除）收在一页里，且该页的数据源始终是知识库本身。
- 删除是「说明影响 → 确认 → 执行 → 回读核对」四步，任一步不成立都不报成功。

**Non-Goals:**

- 不改 `chunking.py`：本次要用的原语都已存在，改动集中在 `11_Q&A_Assistant.py`。
- 不动切分工作台的控件、布局、交互与预览/入库逻辑（两处不可避免的接线下文 D7）。
- 不做存量 chunk 的迁移、重切与批量清理。
- 不引入测试框架；验证仍是一次性脚本与直接断言。

## Decisions

### D1 — 回看整体删除，已入库内容的面收进知识库文档清单

- **选择**：删掉 `get_chunks_visualization()`（`542-630`）、`get_chunks_info()`
  （`653-720`）、`document_choices()`（`951-965`）与两个 UI 回调（`1157`、`1170`）。
- **被否决的替代**：把回看搬到新的知识库文档页，保留一个「查看切分」按钮。
- **否决理由**：使用者明确选择「连功能一起删掉」。附带更正：先前把这次删除描述成
  「顺带清掉死代码」是不准确的——`_get_chunks_from_qdrant()`（`455`）仍被
  `_stored_documents()`（`938`）用来给清单量质量，删掉回看**不会**让它变成死代码。
  所以真正的理由是使用者不再需要这个面，而不是「删了更干净」。
- **代价**：`切分来源可回溯` 失去唯一的观察面，由 D2 接住。

### D2 — 清单增一列「切分来源」，保住 `切分来源可回溯`

- **选择**：`documents_table()` 增加一列，值取 `_doc_split_strategy()`（`632`）与
  `_doc_split_params()`（`640`）拼出的「策略 + 参数摘要」；chunk 里没有记录时显示
  「未记录」。
- **被否决的替代**：随回看一起删掉这条 requirement。
- **否决理由**：使用者要删的是一个界面路径，不是一条能力。`materialize` 写下的
  `split_strategy` / `split_params` 载荷还在（`chunking.py:1378` 一带），两个读回它的
  helper 也还在，保住它只多一列；删掉则那份载荷变成只写不读。
- **代价**：表格从 7 列变 8 列，窄窗口要横向滚动。**无新增 I/O**：`_stored_documents()`
  手里已经有该文档的完整 chunk 列表，两个 helper 只是在内存里取字段。

### D3 — 删除必须带确认闸门与回读核对

- **选择**：确认用一个必须勾选的复选框；执行后调用
  `ck.count_document_chunks(store, path, namespace)`，必须为 0。
- **被否决的替代一**：只信 `ck.purge_document` 的返回值。**否决理由**：它只在
  `store.delete_vectors(ids)` 返回假值时才抛 `IndexingError`；当该文档已经不在库里时
  `ids == []`，函数返回空列表且不报错，此时报「已删除 0 块」就是假成功。而写路径
  `index_document`（`chunking.py:1785-1791`）已经立了「回读核对、对不上就报错」的先例。
  **已实测确认**：`purge_document` 与 `count_document_chunks` 都只经
  `_document_point_ids` 这一个过滤器（`chunking.py:1488-1525`）。
- **被否决的替代二**：两段式按钮（先「删除」，再出现「确认删除」）。**否决理由**：要多
  维护一份控件可见性状态，且在没有浏览器交互的验证脚本里不好断言；复选框是单一布尔量。
- **被否决的替代三**：不做确认。**否决理由**：删除不可撤销，而集合是全身份共享的。
- **代价**：多一次 scroll（`count_document_chunks` 分页扫该文档的 point id），多一个控件。

### D4 — 删除的命名空间安全沿用已有过滤，不新增机制

- **选择**：删除只走 `ck.purge_document(store, file_path, namespace)`。
- **被否决的替代**：在应用层自己拼一遍 `source_path` 过滤条件。
- **否决理由**：那是绕开唯一入口另开一条路径，而 `_document_point_ids` 的 docstring
  已经写明「只按 `source_path` 过滤的话，同一个路径被两个用户各自入库时，这里会取到
  对方的 point——而本函数是删除路径的前置枚举，会删掉对方的数据」。
- **代价**：无。**已实测确认**：`rag_knowledge_base` 里 `docs_yyh` 有 390 块
  （封思敏 327 + 朱光熙 63）、`default` 有 100 块（天地融合 19 + 参考答案 78 + 冀博 2 +
  Hello-Agent 1），按 `rag_namespace` 过滤计数互不干扰。

### D5 — 提问不再要求「本次会话入库过文档」

- **选择**：删掉 `ask()` 的 `current_document` 前置校验（`732-733`）。
- **被否决的替代**：保留门槛。
- **否决理由**：门槛是会话态条件，资源是持久态的。**已实测确认**的失效形态：重启应用后
  `docs_yyh` 下 390 块都在，但 `current_document` 是 `None`，任何提问都被拦成
  「请先加载文档」；删掉一键加载之后，这道门还会逼使用者在提问前重新入库一份已经在库里
  的文档。这与上个变更 D13 修掉的是同一类毛病（会话记录冒充知识库状态）。
- **代价**：没有「只问当前文档」这个语义可失去——`_rag(action="ask")` 从不带文档过滤，
  检索范围本来就是整个命名空间。

### D6 — 不再提供任何不经预览的入库路径

- **选择**：一键加载与它下面的 `_index_with_strategy()`（`237-260`）一起删除。
- **被否决的替代**：把一键加载挪到知识库文档页（上传即入库）。
- **否决理由**：`commit_preview` 的 `is_fresh` 门（`350`）与
  `ChunkingWorkbench.commit` 的 `ChunkingError("尚未预览，无法入库")`（`chunking.py:1737`）
  都建立在「先预览」之上。保留一条绕过它的路径就得在这两处开口子，而
  `预览与入库结果一致`、`参数变化后预览失效` 两条既有契约会同时失去保证。
- **代价**：入库从一次点击变成「上传 → 预览 → 确认」三次；策略必须选一次（有默认值）。

### D7 — 必须动到切分工作台 tab 内的两处，其余一字不动

- **选择**：删掉 `1330` 那句「只想快点用的话，「🏠 开始使用」里的一键加载走的是默认
  策略。」；`1378-1380` 的 `wb_commit_btn.click(...)` 的 `outputs` 去掉已不存在的
  `doc_selector`。
- **被否决的替代一**：留下那行文案。**否决理由**：它指向的入口不存在了，留着就是界面对
  使用者说假话。
- **被否决的替代二**：在 Blocks 根部留一个 `visible=False` 的空下拉框顶替 `doc_selector`，
  好让 `1378-1380` 一个字都不用改。**否决理由**：为了让一行接线保持原样而留一个没有读者的
  幽灵组件，代价远大于改一行 `outputs`——下一个读代码的人会去找谁在用它。
- **代价**：违反「切分工作台一字不动」的字面要求。工作台的控件、布局、交互、预览与入库
  逻辑都不变；被改的是一句已经失效的文案，和一处指向已删组件的输出目标。

### D8 — 随本次失去读者的会话状态一并删除

- **选择**：删掉 `self.stats`（`140-145`）、`loaded_documents` / `document_file_map`
  （`150-151`）、`current_document` / `current_file_path`（`148-149`）、
  `_register_document()`（`262-280`）、`_index_with_strategy()`（`237-260`）。
- **依据**（逐个核过读者，全部在本次被删的对象里）：`stats` 的四个键只被 `get_stats`
  （`881-915`）与 `generate_report`（`999-1042`）读；`loaded_documents` 只被 `get_stats`
  与 `load_document` 读；`document_file_map` 只被 `get_chunks_visualization`（`560`）读；
  `current_document` / `current_file_path` 只被回看、`get_chunks_info`（`662`）与
  `ask()` 的门（`732`）读；`_index_with_strategy` 全项目只在 `398` 被调用一次。
- **被否决的替代**：留着不动。**否决理由**：写而不读的状态会让下一个改动误以为它在被用；
  这个项目刚为「会话记录冒充知识库状态」付过一次代价。
- **代价**：入库不再写「加载了文档《x》」这条情景记忆（`272-279`）。

### D9 — `memory_tool` 保留，本次不清理它的写入

- **选择**：`ask()` 里两条记忆写入（`736`、`755`）原样保留。
- **被否决的替代**：一并删掉。
- **否决理由**：本文件里读记忆的唯一入口 `recall()` **根本没有定义**——`1104` 调用了它，
  而全文件没有 `def recall`（已核对：文件里所有 `def` 的清单里没有它），`generate_report`
  本次又被删。也就是说这些写入在本次改动**之前**就已经没有读者，删与不删都不改变任何
  可观察行为，不属于这三个请求的范围，另开变更更清楚。
- **代价**：`memory_data/memory.db` 继续增长。

### D10 — 两条 requirement 整条换名重写，而不是 MODIFIED 里删 scenario

- **选择**：`提取质量在入库前可见` → `提取质量在预览时可见`；`读路径与写入同命名空间` →
  `读写与删除同命名空间`。两条都以 `## REMOVED Requirements` + `## ADDED Requirements`
  成对出现，正文与保留下来的 scenario 逐字移入新名。
- **被否决的替代一**：在 MODIFIED 块里直接不写那条死掉的 scenario。**否决理由**：工具不允许。
  `openspec validate` 报 `MODIFIED "…" omits scenario(s) the current spec still has`，
  `openspec archive` 的 `specs-apply.js:386` 直接 `throw`（"current spec contains scenario(s)
  not present in the modified block"）。scenario 的比对按名字（`requirement-blocks.js:392`，
  multiplicity-aware），没有豁免标记。
- **被否决的替代二**：REMOVED 与 ADDED 用同一个名字。**否决理由**：`validator.js:339` 把
  「Requirement present in both ADDED and REMOVED」判为 ERROR。
- **被否决的替代三**：留着 scenario 的名字、只改它的正文（比如把 `快速路径给出同一告警` 的
  WHEN 改成切分工作台）。**否决理由**：校验能过，但 spec 里从此留着一条名字与内容不符的
  scenario，正是本项目一直在修的那类「文档说的事不存在」。也考虑过只给
  `回看只呈现本命名空间的内容` 用这招（它的正文对新的文档清单其实仍然成立），但两条死掉的
  scenario 用两套办法处理更难解释。
- **代价**：持久 spec 里这两条 requirement 换了名字，既往变更文档里引用旧名的行文不再对得上；
  能力本身、正文与保留的 scenario 一字未改，验收断言不变。

### D11 — 删除在本次会话内立即生效，不引入任何缓存失效机制

- **选择**：删除就是 `ck.purge_document` 那一次 `store.delete_vectors`，不额外重建管道、
  不清任何缓存、不提示使用者重启。这是使用者对「删除」的定义：不需要重启，但搜索不到那份
  文档的内容了。
- **依据**（已核过库源码）：检索是**实时查 Qdrant**，进程内没有文档索引，也没有检索结果缓存——
  `search_vectors`（`pipeline.py:671`）直接 `store.search_similar(query_vector, where=四个过滤标签)`，
  过滤器里带 `rag_namespace`（`pipeline.py:692-698`）；`delete_vectors`
  （`qdrant_store.py:440-446`）带 `wait=True`，返回时删除已生效，所以紧随其后的检索必然看不到它。
  `RAGTool._pipelines`（`rag_tool.py:60`、`91`）缓存的只是管道对象（内含活着的 Qdrant 客户端），
  不是数据。唯一与查询有关的缓存是嵌入器对**查询向量**的缓存（`pipeline.py:477` 注明了
  「Cache functions removed - using unified embedding with internal caching」）——它缓存的是问题
  文本的向量，与「那份文档还在不在库里」无关。
- **被否决的替代**：删除后主动重建管道或清嵌入缓存。**否决理由**：没有可失效的东西，加了只会
  给下一个读代码的人一个「此处必须重建」的错误暗示；重建管道还要让
  `rag_tool.rag_namespace` 与管道的对应关系重走一遍初始化，平白多一处可能出错的地方。
- **代价**：无代码代价，但这条性质依赖的是外部行为（Qdrant 删除即时可见 + 没有检索缓存），
  不是本地代码的不变量。所以 spec 里用一条 scenario（`删除后同一会话内立即检索不到`）钉住它，
  验证里有一次「删前查得到、删后查不到」的比对（tasks 7.4）——将来谁加了检索缓存都会被它拦住。

## 不能破坏的既有契约

本次不改这些行为，逐条指回 spec：

| 契约 | requirement |
|---|---|
| 入库 chunk 带齐四个过滤标签，入库后可被检索 | `document-chunking` / 入库 chunk 保持可检索 |
| 入库先清该文档旧 chunk，再写新的；跨命名空间不互相影响 | `document-chunking` / 提交入库时清理旧切分 |
| 预览之后改参数或换文件，拒绝入库并要求重新预览 | `document-chunking` / 参数变化后预览失效 |
| 入库的就是预览过的同一份 | `document-chunking` / 预览与入库结果一致 |
| 预览不写知识库 | `document-chunking` / 入库前预览 |
| 清单与度量取自知识库，不取会话记录 | `document-chunking` / 已入库文档的提取质量可逐个查看 |
| 覆盖率用位置区间并集，不用字符数之和 | `document-chunking` / 切分不丢内容 |
| 清洗只做三类定点改动，不用 NFKC | `document-chunking` / 提取文本的确定性清洗 |
| 块大小按 token 计量，界面不得标成字符 | `document-chunking` / 切分参数单位标注准确 |
| 读操作与写入同命名空间，不依赖库的默认值 | `rag-namespace-consistency` / 读写与删除同命名空间（原 `读路径与写入同命名空间`，见 D10） |
| 初始化时报告命名空间与块数，空命名空间要提示 | `rag-namespace-consistency` / 命名空间状态在初始化时可见 |

## Risks / Trade-offs

按失效形态排序，静默劣化在前：

1. **[静默劣化] 删除报成功但内容还在**：库对写入/删除失败一路照常返回（`chunking.py:1782-1784`
   记着同一类现象：编码超时塞零向量后照常报告成功）。→ **缓解**：D3 的回读核对；
   核对不通过时报失败并给出剩余条数。
2. **[静默劣化] 删除报成功但删了 0 条**：选择器指向的文档已被别处删除或列表过期时，
   `purge_document` 返回 `[]` 且不抛异常。→ **缓解**：执行前核对该 `source_path` 是否仍在
   `ck.list_documents` 的结果里，不在就报「已不在知识库中」。
3. **[静默劣化] 同名不同路径的两份文档在选择器里长得一样**：文档身份是 `source_path`，
   而库里多数 `source_path` 是已经消失的 Gradio 临时路径；两份都叫 `论文.pdf` 时只靠文件名
   分不出谁是谁，删除又不可撤销。→ **缓解**：标签带 chunk 数；删除前的确认提示与删除后的
   报告都回显完整 `source_path`。残余风险已知：使用者仍可能看错，但不会在信息缺失的情况下删。
4. **[报错] 漏改 `wb_commit_btn` 的 `outputs`**：`create_gradio_ui` 构造界面时会 `NameError`，
   应用起不来。→ 不是静默的，启动即失败；任务里有一条「起得来 + 入库一次」的验证。
5. **[静默劣化] 清单里的「未记录」被读成「默认策略」**：存量 chunk 没有 `split_strategy`。
   → **缓解**：显示「未记录」而不是默认值，spec 里有一条 scenario 钉住这一点。
6. **[行为变化] 提问不再被门槛拦住**：空命名空间下会走完一次检索再答「没找到」。
   → 初始化状态串已经显示块数，且 `空命名空间不串到其他命名空间` 仍要求如实报告。
7. **[体验] 入库从一次点击变成三次**：这是 D6 有意保留的性质，不是待修的问题。

## Migration Plan

- **无数据迁移**。本次不改 `chunking.py`，不重切、不迁移、不清理任何存量 chunk；
  `docs_yyh`（390 块）与 `default`（100 块）的内容不因改动而变化，删除功能只在使用者
  显式操作时生效。
- **回退**：本目录不是 git 仓库，没有版本控制可回退。动手前把 `11_Q&A_Assistant.py`
  另存一份（例如 `11_Q&A_Assistant.改造前.py`）；回退即还原该文件。
- **孤儿数据**：磁盘上已有的 `notes_*.json` 留在原处不删，回退后笔记页仍能读到。

## Open Questions

- 知识库文档数增长到几十份之后，删除选择器的下拉框是否还够用（Gradio 的 Dropdown 支持
  输入过滤，但标签里带 chunk 数会让匹配变复杂）。这不改变 spec、方案与任务拆分，等实际
  文档数变多再看。
