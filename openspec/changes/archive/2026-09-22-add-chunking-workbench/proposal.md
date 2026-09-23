# Proposal

## Why

文档切分策略目前写死在 `hello_agents` 库内部（`memory/rag/pipeline.py` 的
`load_and_chunk_texts` 恒走 Markdown 结构感知这一条路径），应用层没有任何选择余地；
而现有的「查看文档切分」是**入库之后**从 Qdrant 回读的，用户必须先接受一个不可见的
切分结果，才能看到它。切分质量直接决定 RAG 检索质量，却成了整个流程里唯一无法干预、
无法预览的一环。

本次变更把切分决策上移到应用层：预览所见即入库所得。

## What Changes

- 新增独立标签页「🔪 切分工作台」，承载 `上传 → 选策略 → 调参 → 预览 → 确认入库` 的完整闭环
- 新增三种可选的切分策略：
  - **段落分割**：按空行段落聚合，受块大小与重叠约束
  - **Markdown 结构感知**：按标题层级 + 段落切分（等价于库当前行为，作为基线）
  - **语义分割**：基于 embedding 的相邻语义距离切分，真实调用向量模型
- 切分预览不再从数据库回读，而是直接渲染**即将入库的那份 chunk 列表**，
  预览与入库共用同一份数据，结构上杜绝二者不一致
- 语义分割提供「语义敏感度」滑块，并绘制相邻语义距离曲线，可视化每个切点为何落在该处
- 入库前自动清除该文档在向量库中的旧 chunk，再写入新 chunk，避免换策略后新旧切分共存污染检索
- 保留「🏠 开始使用」原有的「加载文档」按钮，继续走默认策略（Markdown 结构感知）的一键快速路径
- 修正现有可视化中把 token 标注为「字符」的单位错误
- 入库 chunk 的 metadata 增加切分来源标注（策略名与参数），便于入库后回溯
- 修正知识库问答读路径的命名空间：库的 `RAGTool.run` 在未显式传 namespace 时取写死的默认值
  `"default"`，与写入所用的 `docs_{user_id}` 不一致，导致提问检索到其他命名空间下的文档
  而非本身份的文档；读、回看、统计、清理枚举四处统一带上本助手的命名空间
- 助手初始化时报告实际使用的命名空间与其下已入库的 chunk 数量，使「命名空间里没有内容」
  当场显形，而不是等提问检索不到才发现

## Capabilities

### New Capabilities
- `document-chunking`: 文档切分策略的选择、参数调节、入库前预览、以及将选定切分结果提交入库（含旧 chunk 清理）的完整行为契约
- `rag-namespace-consistency`: 知识库问答的各读操作与写入使用同一命名空间，以及该命名空间状态在助手初始化时的可见性

### Modified Capabilities
<!-- 项目当前 openspec/specs/ 为空，无既有 capability 需要修改 -->

## Impact

**代码**
- `11_Q&A_Assistant.py`（现 1196 行）：新增切分工作台标签页与相应回调；`load_document`
  的入库路径需要改写为「本地切分 → 清理旧 chunk → `index_chunks`」；读路径（`ask` /
  `stats` / `_get_chunks_from_qdrant`）统一经 `_rag()` 注入命名空间，初始化状态串补上
  命名空间与已入库块数
- 新增切分器模块与渲染模块（具体文件划分见 design.md），这会打破 chapter8
  目录「单文件脚本、互不 import」的既有惯例，属有意为之

**依赖**
- 复用 `hello_agents.memory.rag.pipeline` 的 `_convert_to_markdown`（私有函数，需包一层）
  与 `index_chunks`（公开的安全入库入口，自行注入检索所需的过滤标签）
- 复用 `hello_agents.memory.embedding.get_text_embedder()` 做语义分割，确保与入库同一向量空间
- 复用 `hello_agents.memory.storage.qdrant_store.QdrantVectorStore.delete_vectors()` 清理旧 chunk

**数据**
- Qdrant collection `rag_knowledge_base`（`RAGTool.__init__` 的默认值，也是本应用实际使用的
  那个；`.env` 里的 `QDRANT_COLLECTION=hello_agents_vectors` 并不被 `RAGTool` 读取）：同一
  文档的 chunk 集合在换策略后被整体替换，chunk id 会变化。已入库文档在首次经新流程重新入库
  前保持原状，不受影响
- 该集合为全部命名空间共享，chunk 以 `rag_namespace` 载荷标签区分归属；因此应用层的读、
  回看、统计与清理枚举都必须带上该标签，否则跨命名空间命中

**风险**
- `start` / `end` 必须保持为 markdown 原文的真实字符偏移，否则
  `compute_graph_signals_from_pool`（1600 字符近邻窗口）与 `expand_neighbors_from_pool`
  会静默劣化检索排序
- 清理旧 chunk 与新 chunk 写入之间非原子，中途失败会导致该文档暂时从知识库消失
