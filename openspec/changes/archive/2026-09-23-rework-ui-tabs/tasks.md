# Tasks

## 1. 动手前的准备

- [x] 1.1 把 `11_Q&A_Assistant.py` 另存一份为 `11_Q&A_Assistant.改造前.py`。
      验证：`ls -l` 两个文件，字节数相同。本目录不是 git 仓库，这是唯一的回退手段（design.md / Migration Plan）。
      **已完成**：两份都是 63890 字节。
- [x] 1.2 记录改动前的基线读数（**只读**，不写任何命名空间）：用 `C:/Users/yyh15/.conda/envs/HelloAgent/python.exe` 跑一段一次性脚本，`ck.get_store(RAGTool(rag_namespace=...))` 之后断言
      `ck.list_documents(store, "docs_yyh")` 返回 2 份文档、块数合计 390（封思敏 327 + 朱光熙 63），
      且 `namespace_chunk_count` 口径下 `default` 为 100。把每份文档的 `source_path` 与块数抄进本任务下，
      改动后要拿它比对（7.3）。
      **已完成，但实测读数与本任务写的预期不一致**——知识库是活动数据，任务写下之后使用者又入库过
      （且切分参数不同，所以同一份 PDF 的块数也变了）。**以实测为准**，7.3 按下面这组比对：

      | 命名空间 | 份数 | 块数 | 逐份 |
      |---|---|---|---|
      | `docs_yyh` | 5 | 266 | 语义通信研究现状与发展趋势 42 / 人工智能物联网中面向智能任务的语义通信方法 25 / 遥感星群任务智能规划机制_朱光熙 21 / 低轨巨型星座网络智能路由技术研究_封思敏 87 / 智能化卫星互联网运维与管理_唐斯琪 91 |
      | `default` | 4 | 100 | 天地融合网络智能组网体系架构研究 19 / Hello-Agent.md 1 / Extra01-参考答案.md 78 / 冀博-从SDD到确定性质量门禁 2 |
      | `docs_qualitycheck` | 0 | 0 | ——（本次的试验田，应为空） |

      五份的 `source_path` 都是 `C:\Users\yyh15\AppData\Local\Temp\gradio\<hash>\<文件名>` 形式。
      design.md D4 / D11 引用的「390 块（封思敏 327 + 朱光熙 63）」是规划时的读数，同样已过期；
      那两处的结论（按 `rag_namespace` 过滤互不干扰）与本次改动无关，不据此改 design。
      附带发现（与本次无关，供 7.x 脚本注意）：裸脚本里 `RAGTool(...)` 不走 `.env`，会打印
      「❌ RAG工具初始化失败: API密钥…」——store 仍然可用，但脚本里凡是要走 LLM 的验证（6.3、7.4）
      必须先 `load_dotenv()`。

## 2. 删掉「🏠 开始使用」里的加载与查看切分

- [x] 2.1 删掉 `PDFLearningAssistant.load_document()`（`11_Q&A_Assistant.py:378-454`）。
      验证：`grep -n "def load_document" 11_Q&A_Assistant.py` 无输出。
- [x] 2.2 删掉 `_index_with_strategy()`（`11_Q&A_Assistant.py:237-260`）——它只被 `load_document` 调用过（`398`）。
      验证：`grep -n "_index_with_strategy" 11_Q&A_Assistant.py` 无输出。
- [x] 2.3 删掉 `get_chunks_visualization()`（`542-630`）、`get_chunks_info()`（`653-720`）、
      `document_choices()`（`951-965`）。
      验证：`grep -n "def get_chunks_visualization\|def get_chunks_info\|def document_choices"` 无输出；
      且 `grep -c "_get_chunks_from_qdrant"` 仍等于 2（定义处 + `_stored_documents` 里的调用）——
      这个方法**必须留着**，文档清单的质量度量靠它（design.md D1）。
- [x] 2.4 删掉 `view_chunks_ui()`（`1157`）与 `get_chunks_text_ui()`（`1170`）。
      验证：`grep -n "def view_chunks_ui\|def get_chunks_text_ui"` 无输出。
- [x] 2.5 改 `commit_preview()` 的返回值：成功与失败分支都只返回一个字符串，
      去掉 `gr.update(choices=self.document_choices(), value="当前文档")`（`375`、`354`、`362`、`364`、`366`）；
      `workbench_commit()`（`1213`）的 `Tuple[str, Any]` 标注同步改。
      验证：`grep -n "document_choices" 11_Q&A_Assistant.py` 无输出。
- [x] 2.6 删掉「🏠 开始使用」tab 里的「加载文档」段（`1280-1289`：`file_upload`、`load_btn`、`load_output`）
      与「🔍 查看文档切分」段（`1291-1322`：`doc_selector`、`viz_btn`、`text_btn`、`chunks_output`、
      `refresh_doc_choices` 及两处 click 接线）。
      验证：`grep -n "file_upload\|load_btn\|load_output\|doc_selector\|viz_btn\|text_btn\|chunks_output\|refresh_doc_choices"`
      只剩 `wb_commit_btn.click` 那一行里的 `doc_selector`（由 3.2 处理）；该 tab 内只剩
      `user_id_input`、`init_btn`、`init_output` 三个组件。

## 3. 「🔪 切分工作台」tab 内两处不可避免的改动

这一组是本次唯一会碰到该 tab 代码的两处，做完后 diff 应当只有两行；该 tab 的控件、布局、
交互、预览与入库逻辑一律不动（design.md D7）。

- [x] 3.1 删掉说明里那句「只想快点用的话，「🏠 开始使用」里的一键加载走的是默认策略。」（`1330`）。
      验证：`grep -n "一键加载" 11_Q&A_Assistant.py` 无输出；该 tab 的 Markdown 其余三行逐字未变。
- [x] 3.2 把 `wb_commit_btn.click(workbench_commit, inputs=wb_inputs, outputs=[wb_status, doc_selector])`（`1378-1380`）
      的 `outputs` 改成 `[wb_status]`。
      验证：`grep -n "doc_selector" 11_Q&A_Assistant.py` 无输出；
      `python -c "import ast;ast.parse(open('11_Q&A_Assistant.py',encoding='utf-8').read())"` 无报错。
- [x] 3.3 应用起得来、工作台还能入库（集成验证，覆盖 2.x 与 3.x）。
      以 user_id=`qualitycheck` 初始化（命名空间 `docs_qualitycheck`），上传一份小文本文件 →
      「预览切分」→「确认入库」，断言状态串形如 `✅ 已入库：N 个 chunk`。
      随后用一次性脚本断言 `ck.count_document_chunks(store, <该文件路径>, "docs_qualitycheck") == N`；
      收尾调 `ck.purge_document(store, <该文件路径>, "docs_qualitycheck")` 并断言该命名空间块数为 0。
      **不写 `docs_yyh`、不写 `default`。**
      **已完成**（脚本 `_verify_33.py`，跑完即删）：`✅ 界面构建成功，组件数 77`、
      `预览状态: ✅ 预览完成：语义感知切分，共 1 个 chunk`、`入库状态: ✅ 已入库：1 个 chunk`、
      `✅ 回读一致：1 块`、`✅ 清理完毕：docs_qualitycheck 已回到 0 块`。
      界面能构建这一条同时证明 2.5 / 3.2 把 `commit_preview` 的返回类型与
      `wb_commit_btn` 的输出列表改对了——接线错的那一版在 `create_gradio_ui()` 就会抛。

## 4. 删掉「📝 学习笔记」

- [x] 4.1 删掉 `add_note()`（`817`）、`get_notes()`（`848`）、`_load_notes()`（`160`）、`_save_notes()`（`183`）、
      `add_note_ui()`（`1137`）、`view_notes_ui()`（`1148`）。
      验证：`grep -n "def add_note\|def get_notes\|def _load_notes\|def _save_notes\|def add_note_ui\|def view_notes_ui"` 无输出。
      **已完成**：六个定义全部删掉，`grep` 无输出。
- [x] 4.2 删掉 `__init__` 里的 `self.notes_file` / `self.notes`（`153-155`）。
      验证：`grep -n "notes" 11_Q&A_Assistant.py` 无输出。
      **已完成**，但验证用的 grep 词太宽：删完仍有 4 处 `notes`，**都不是笔记功能**，而是
      `preview_chunks()` 里给质量/覆盖率摘要起的局部变量名（`260`、`264`、`265`、`266`，
      `notes = [...]` / `notes = [n for n in notes if n]`）。那是切分工作台预览的代码，
      本次不许动（design.md D7），所以不改名。**该断言的真正对象是笔记持久化状态**：
      `grep -c "self\.notes\|notes_file" 11_Q&A_Assistant.py` = 0 ✓。
- [x] 4.3 删掉「📝 学习笔记」tab（`1462-1481`）与页首说明里的「📝 学习笔记记录和学习历程回顾」一行（`1264`）。
      验证：`grep -n "学习笔记" 11_Q&A_Assistant.py` 无输出；磁盘上已存在的 `notes_*.json` **不删**
      （design.md / Non-goals）。
      **已完成**：tab（原第 957-977 行，含 `note_btn` / `view_notes_btn` / `note_output` /
      `notes_display` 与两处接线）与页首那一行都已删；页首同时去掉了「📊 学习报告生成」一行，
      补上一行「📚 知识库文档」（与 5.4 的新 tab 对应）。磁盘上的 `notes_yyh.json` 原样保留。
      残留 2 处 `学习笔记` 字样在 `get_stats`（`537`）与 `generate_report_ui`（`789`）里，
      两个函数都由 5.4 删除——5.4 之后本验证的 grep 才完全为空。

## 5. 「📊 学习统计」换成「📚 知识库文档」

- [x] 5.1 给 `documents_table()`（`967-997`）加一列「切分来源」：值取 `_doc_split_strategy()`
      与 `_doc_split_params()`，策略用 `ck.STRATEGIES[s]["label"]`，参数用 `ck.param_specs(s)` 的
      `unit` 拼「值+单位」；chunk 里没记 `split_strategy` 时显示「未记录」，**不得**回落到默认策略。
      验证：跑 `PDFLearningAssistant("yyh").documents_table()`（**只读** `docs_yyh`），
      断言表头是 8 列、行数等于文档数（2），并把实际输出抄进本任务下；若某行是「未记录」，
      用 `_get_chunks_from_qdrant` 核对该文档的 chunk 确实没有 `split_strategy` 字段。
      **已完成**（脚本 `_verify_51.py`，跑完即删）：表头 8 列、数据行 5 行，
      与 `ck.list_documents(store, "docs_yyh")` 的 5 份 / 266 块一致（任务里写的 2 份同 1.2，
      同样是过期读数）。五行**没有一行是「未记录」**——五份都是本次改造前就带着
      `split_strategy` 入库的，所以「不得回落到默认策略」这条只在代码上得到保证，
      实测没有触发；`_get_chunks_from_qdrant` 的那个核对分支因此没跑。实际输出：

      | 文档 | Chunk 数 | 切分来源 | 私用区 | 全角拉丁 | 空壳表格率 | 偏移校验 | 判定 |
      | --- | ---: | --- | ---: | ---: | ---: | :---: | --- |
      | 人工智能物联网中面向智能任务的语义通信方法_NormalPdf.pdf | 25 | Markdown 结构感知 · 块大小800token、重叠大小80token | 0.00% | 0.00% | 51.1% | ✅ | ✅ 正常 |
      | 低轨巨型星座网络智能路由技术研究_封思敏.pdf | 87 | Markdown 结构感知 · 块大小800token、重叠大小80token | 4.12% | 10.46% | 51.8% | ✅ | ⚠️ 含噪声 |
      | 基于多模态观测需求信息的遥感星群任务智能规划机制_朱光熙.pdf | 21 | Markdown 结构感知 · 块大小550token、重叠大小80token | 0.00% | 0.00% | 28.1% | ✅ | ✅ 正常 |
      | 大模型使能的语义通信研究现状与发展趋势.pdf | 42 | Markdown 结构感知 · 块大小800token、重叠大小80token | 0.01% | 0.00% | 51.8% | ✅ | ✅ 正常 |
      | 智能化卫星互联网运维与管理：现状与机遇_唐斯琪.pdf | 91 | 语义分割 · 语义敏感度75%、最小块200字符、最大块1600字符 | 0.00% | 0.00% | 46.4% | ✅ | ✅ 正常 |

      附带一条与 6.3 互证的观察：这一列之外的质量列把 封思敏 那份标成
      `⚠️ 含噪声`（私用区 4.12%、全角拉丁 10.46%，全库唯一一份），正是 6.3 里
      「这个词取不到」的原因；表里另外四份都是 `✅ 正常`。
- [x] 5.2 新增 `delete_document(self, source_path: str) -> str`：先核对 `source_path` 仍在
      `ck.list_documents(store, self.rag_tool.rag_namespace)` 的结果里——不在则返回「该文档已不在知识库中」，
      **不调用**删除；在则调 `ck.purge_document(store, source_path, self.rag_tool.rag_namespace)`，
      随后断言 `ck.count_document_chunks(store, source_path, ns) == 0`，不为 0 时返回失败并给出剩余条数
      （MUST NOT 报成功）。成功返回含文档名与删除块数的字符串。
      验证：见 5.5 与 7.2；`grep -n "store.add_vectors\|store.delete_vectors" 11_Q&A_Assistant.py` 无输出
      （删除只走 `purge_document`）。
      **已完成**：grep 无输出，删除路径只经 `ck.purge_document`。行为验证在 5.5（跨命名空间/跨文档）
      与 7.2（界面两次点击）里跑。
- [x] 5.3 新增 UI 回调 `delete_document_ui(source_path, confirmed)`：`confirmed` 为假时返回
      「❌ 请先勾选确认…」且**不调用** `delete_document`；为真时先回显完整 `source_path`，
      再调用 `delete_document`，最后刷新表格与选择器。
      验证：7.2 里那两次点击（未勾选 / 已勾选）的返回串。
      **已完成**，两次点击的返回串见 7.2：未勾选返回
      `❌ 未勾选确认，没有删除任何内容。` + 即将删除的是《…》共 N 个 chunk + 完整路径，
      且该文档块数不变；已勾选返回 `✅ 已删除…` 并带回刷新后的表格与选择器。7.4 又用同一个
      回调删了一次（`delete_document_ui(path, True)`），同样当场生效。
- [x] 5.4 把「📊 学习统计」tab（`1483-1492`）换成「📚 知识库文档」：一个「刷新列表」按钮渲染
      `documents_table()`；一个文档选择器（choices 来自 `_stored_documents()`，标签形如
      `{文件名} · {块数} 块 · {判定}`，取值是 `source_path`）+ 一个必须勾选的确认复选框 + 「删除」按钮。
      同时删掉 `get_stats()`（`881`）、`get_stats_ui()`（`1187`）、`generate_report()`（`999`）、
      `generate_report_ui()`（`1236`）。
      验证：`grep -n "def get_stats\|def generate_report\|learning_report" 11_Q&A_Assistant.py` 无输出；
      根目录不再出现新的 `learning_report_*.json`（跑一次应用后 `ls learning_report_*.json` 与改动前一致）。
      **已完成**：四个函数删掉，grep 无输出；新 tab 的接线是
      `docs_btn.click(refresh_docs_ui, outputs=[docs_table, del_selector])` 与
      `del_btn.click(delete_document_ui, inputs=[del_selector, del_confirm], outputs=[del_output, docs_table, del_selector, del_confirm])`。
      4.3 留在 `get_stats` / `generate_report_ui` 里的那 2 处「学习笔记」字样随本任务消失，
      现在 `grep -n "学习统计\|学习笔记"` 也是空的（4.3 的 grep 至此才完全成立）。
      根目录 `learning_report_*.json`：改动前后都是 0 个，没有新文件落地。
- [x] 5.5 跨文档与跨命名空间的删除隔离验证（**写 `docs_qualitycheck`**）：
      用一次性脚本往 `docs_qualitycheck` 写两份文档（各 2 块与 3 块），其中一份的 `file_path`
      **故意**取 1.2 记下的、`docs_yyh` 里那份文档的 `source_path`；然后删掉这一份，断言：
      `docs_qualitycheck` 里它归零、另一份仍是 3 块、`docs_yyh` 里那个路径的块数与基线的 390 一致。
      收尾清空 `docs_qualitycheck`（`purge_document` 两份）并断言该命名空间块数为 0。
      补充（实现时确认）：`ck.materialize` 的 `file_path` 只进 payload 不读文件；若它读文件，
      就把测试用的小文件复制到那个路径，用完删掉副本。
      **已完成**（脚本 `_verify_55.py`，跑完即删）。补充那条也确认了：
      `materialize` 只把 `file_path` 写进 `metadata["source_path"]`（`chunking.py:1427`），
      不读文件，所以不需要往 Gradio 临时目录里放副本。基线按 1.2 注里的实测值取
      （1.2 写的 390 块同样已过期）：

      - 写入：A 借的是 `docs_yyh` 里封思敏那份的 `source_path`（两个命名空间里**同一个完整路径**，
        正是 `_document_point_ids` 双条件过滤要挡的情形）2 块，B 是新路径 3 块。
      - 删除 A 走的是应用自己的 `assistant.delete_document(path_a)`，返回
        `✅ 已删除《低轨巨型星座网络智能路由技术研究_封思敏.pdf》的 2 个 chunk，此后检索不再返回它的内容（不需要重启）。`
      - 隔离成立：`docs_qualitycheck` 里 A 归零、B 仍 3 块、`docs_yyh` 里同路径那份仍 **87** 块、
        `docs_yyh` 总量仍 **266** 块（删除没有越界到另一个命名空间）。
      - 删完 `list_documents(store, "docs_qualitycheck")` 只剩 B；再删一次 A 被第一步挡下
        （`❌ 该文档已不在知识库中，未执行删除（列表可能已经过期，请点「刷新列表」后再试）。`），
        且 B 仍是 3 块——`purge_document` 对不存在的文档返回空列表不报错，这个前置核对挡住的
        正是「已删除 0 块」式的假成功。
      - 收尾：两份都 `purge_document`，`docs_qualitycheck` 回到 0 块，`docs_yyh` 仍是 266 块。

## 6. 清理失去读者的状态与提问门槛

- [x] 6.1 删掉 `self.stats`（`140-145`）与 `ask()` 里的 `stats["questions_asked"] += 1`（`764`）。
      验证：`grep -n "self.stats" 11_Q&A_Assistant.py` 无输出。
      **已完成**，grep 无输出。
- [x] 6.2 删掉 `loaded_documents` / `document_file_map`（`150-151`）、`current_document` /
      `current_file_path`（`148-149`）、`_register_document()`（`262-280`），以及 `commit_preview` 里
      对它的调用（`368`）。
      验证：`grep -n "loaded_documents\|document_file_map\|current_document\|current_file_path\|_register_document"`
      无输出。
      **已完成**，五个词一起 grep 无输出；`commit_preview` 里那一行调用删掉后
      `file_path` 形参仍有读者（`is_fresh(file_path, ...)`），没有留下悬空参数。
- [x] 6.3 删掉 `ask()` 的 `current_document` 前置校验（`732-733`）。
      验证：`grep -n "请先加载文档" 11_Q&A_Assistant.py` 无输出；再以 user_id=`yyh` 初始化
      （**只读** `docs_yyh`）、**不上传任何文件**直接问「有哪些系统模型」，断言拿到答案且来源是封思敏
      那份文档，而不是「请先加载文档」。单次提问走 MQE + HyDE，约 10–20 秒。
      **已完成，但要对第二条断言记一笔实测差异**（脚本 `_verify_63.py` / `_probe_63.py`，跑完即删）：

      - 门槛确实没了：`assistant` 上已无 `current_document` / `current_file_path` /
        `loaded_documents` / `document_file_map` / `stats` 五个属性；`ask("有哪些系统模型")`
        返回的是正常问答正文（`⚡ 检索: 8653ms | 生成: 8881ms`），不含「请先加载文档」，
        且检索经 `_rag` 走的就是 `docs_yyh`（`本命名空间块数: 266`）。
      - **但这次提问的来源不是封思敏**：5 条来源是朱光熙 0.777 / 唐斯琪 0.771 / 唐斯琪 0.767 /
        朱光熙 0.764 / 朱光熙 0.755，模型也如实回答「给定上下文不足以回答」。
        这不是本次改动引入的，而是本任务写下时的预期已经过期：那条预期来自 1.2 注里记下的
        旧读数（`docs_yyh` 只有封思敏 327 + 朱光熙 63），而库里现在是 5 份 / 266 块、
        且切分参数不同。同一批数据在 1.2 已经对不上，此处同理。
      - 封思敏那份**确实在这个命名空间里、也确实取得到**（不是丢了）：`_get_chunks_from_qdrant`
        回读 87 块；拿它自己第 1 块的正文当查询走 `_rag(action="search")`，5 条里 3 条是它
        （0.797 / 0.795 / 0.791）。取不到的那个问题是「系统模型」这个词在
        **这份 PDF 的提取文本里本来就变形了**——第 1 块正文是
        `密级 公开 | | | 耆 | | Ａ |`，正文里是 `ＴＤ －ｅｒｒｏｒ`、`３－２５`、
        `［ ３３ ］`（全角拉丁 + 私用区 + 表格骨架），与 design/Non-goals 里
        「提取质量另开变更」记的是同一件事。
      - 所以本任务判定的**行为**（不上传文件也能问、不再被会话态拦下）达成；
        来源指向哪份文档由库里的数据与提取质量决定，不在本次范围内。

## 7. 端到端验证

- [x] 7.1 起一次应用，确认 tab 只有五个——「🏠 开始使用」「🔪 切分工作台」
      「💬 基于知识库智能问答」「🧠 基于大模型自由问答」「📚 知识库文档」，
      且「🏠 开始使用」里只有用户ID 输入框与「初始化助手」按钮。
      **已完成**（脚本 `_verify_7.py`，跑完即删）：`gr.Blocks` 上恰好 5 个 `Tab`，
      label 与顺序逐字符合；「🏠 开始使用」的叶子控件是
      `["Textbox('用户ID')", 'Button(None)', "Textbox('初始化状态')"]`——只有这三个，
      没有上传框、没有下拉框、没有「查看切分」按钮。另外全界面已无任何 label 含
      「加载文档」「查看文档切分」「一键加载」的控件。
- [x] 7.2 端到端走一遍（user_id=`qualitycheck`，命名空间 `docs_qualitycheck`）：
      初始化 → 切分工作台入库一份小文档 → 「📚 知识库文档」刷新列表看到它（切分来源列不是「未记录」）
      → 不勾选确认点删除，断言被拒且该文档块数不变 → 勾选后删除，断言该文档块数归零、表格里不再有它。
      **已完成**，走的是**界面上真正接线的回调**（从 `demo.fns` 里取出
      `init_assistant` / `workbench_preview` / `workbench_commit` / `refresh_docs_ui` /
      `delete_document_ui`），不是另调方法：

      - 初始化：`✅ 助手已初始化 (用户: qualitycheck | 命名空间: docs_qualitycheck)` +
        空库告警（此时还没入库，告警是对的）。
      - 预览 → `✅ 预览完成：Markdown 结构感知，共 1 个 chunk` + `⚠️ 内容覆盖率 98.44%（未覆盖 1 字）`；
        入库 → `✅ 已入库：1 个 chunk。`；回读 1 块。
      - 刷新列表那一行：`| _verify_72_样本文档.txt | 1 | Markdown 结构感知 · 块大小800token、重叠大小80token | 0.00% | 0.00% | 0.0% | ✅ | ✅ 正常 |`
        ——「切分来源」有值，不是「未记录」；选择器标签 `_verify_72_样本文档.txt · 1 块`（质量正常，
        按设计不加告警字）。
      - 未勾选：`❌ 未勾选确认，没有删除任何内容。` + 回显《…》共 1 个 chunk + 完整路径，
        块数仍是 1。
      - 已勾选：`✅ 已删除《_verify_72_样本文档.txt》的 1 个 chunk，此后检索不再返回它的内容（不需要重启）。`
        块数归零，刷新后的表格里不再有它。
- [x] 7.3 与 1.2 的基线逐项比对，断言本次改动没有碰过存量数据：`docs_yyh` 仍是 2 份 / 390 块，
      `default` 仍是 100 块，`docs_qualitycheck` 的块数是 0（清理干净）。
      **已完成**，按 1.2 注里记下的**实测**基线比对（任务里的 2 份 / 390 块是过期读数，
      1.2 已经记过这件事）：`docs_yyh` **5 份 / 266 块** ✓、`default` **4 份 / 100 块** ✓、
      `docs_qualitycheck` **0 份 / 0 块** ✓。本次全部验证脚本对存量的写入是 0。
- [x] 7.4 「删除后同一会话内立即检索不到」的验证（**写 `docs_qualitycheck`**；依赖 5.2 的
      `delete_document`，接在 7.2 之后跑）。做一份含一句自造独有内容的文本文件入库
      （例如正文里放一句「紫电青霜测试哨句」，确保这句话不会出现在库里任何别的文档里），
      然后分两层断言：
      （a）**确定性判据**——用应用自己的读漏斗，删前
      `assistant._rag(action="search", query="紫电青霜测试哨句", limit=5)` 返回该文档的内容，
      删除后同一次调用返回检索不到任何内容（不走 LLM，不受模型措辞影响）；
      （b）**端到端判据**——在「💬 基于知识库智能问答」问这句话，删前答得出且来源是这份文件；
      **不重启应用**，到「📚 知识库文档」删掉它，再问同一句话，断言回答里不再引用该文档、
      且表明未从知识库检索到相关内容。两次提问走 MQE + HyDE，各约 10–20 秒。
      收尾断言 `docs_qualitycheck` 的块数为 0。
      **已完成**，两层判据都过，全程同一个进程、**没有重启**：

      - （a）删前 `search` 第 1 条就是
        `文档: **_verify_74_紫电青霜.txt** (相似度: 0.801)`，正文即那句哨句；
        删后同一次调用返回 `🔍 未找到与 '紫电青霜测试哨句' 相关的内容`。
      - （b）删前 `ask` 引用该文档（`🟢 [1] _verify_74_紫电青霜.txt (相似度: 0.806)`，
        `⚡ 检索: 15645ms | 生成: 3115ms`）；删除动作走「📚 知识库文档」页的
        `delete_document_ui(path, True)`；删后同一句话的回答是
        `🤔 抱歉，我在知识库中没有找到与「紫电青霜测试哨句」相关的信息。` + 三条建议，
        不再引用该文档。
      - 收尾 `docs_qualitycheck` 0 块。
      - 这两条一起把 design.md D11 的前提坐实了：检索每次现查 Qdrant、进程内没有文档索引
        与检索缓存，所以删除当场生效，不需要重启。7.2 里那句状态串
        「此后检索不再返回它的内容（不需要重启）」不是一句安慰话。

## 8. 文档同步

- [x] 8.1 更新 `openspec/config.yaml` 架构表里 `document_choices()` 那一行：它描述的
      「两条入库路径拼出两种形状的文档下拉框」在本次之后只剩一条入库路径，而该函数已不存在。
      验证：`grep -n "document_choices" openspec/config.yaml` 无输出，且表内其余四行逐字未变。
      **已完成**，`grep` 无输出，其余四行（`extract_markdown` / `materialize` /
      库的 `index_chunks` / `_rag`）逐字未动，`yaml.safe_load` 仍可解析（`context` 7081 字节）。
      **一处自行决定，请使用者过目**：那一行没有直接删掉，而是换成了本次新出现的同类入口
      `| PDFLearningAssistant.delete_document() | 跳过删除前的核对与删除后的回读——报「已删除 0 块」的
      假成功，或删到别的命名空间 |`。理由是表头写着「五个「唯一入口」」，单删会剩四行与这句话对不上；
      且 design.md D3/D4 正是把「删除只走 `purge_document`、删前核对、删后回读」立成一条不能被绕开的
      路径，与 `_rag()` 那一行是同一类告警。若更想只删不补，把该行去掉、表头「五个」改「四个」即可。
