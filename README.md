# Knowledge-Q-A

文档知识库问答助手。基于 [datawhalechina/Hello-Agents](https://github.com/datawhalechina/Hello-Agents)
第 8 章的示例改造，Gradio 界面 + 向量库检索，重点是把**切分**这一步从库内固定行为
变成入库前可干预、可预览的应用层选择。

## 功能

- **🔪 切分工作台** — 三种切分策略可选（段落分割 / Markdown 结构感知 / 语义分割），
  调参后先预览 chunk 与统计，确认无误再入库；预览所见即入库所得，改参数后必须重新预览
- **知识库文档清单** — 列出当前命名空间下已入库的文档、块数、切分来源与提取质量，
  可删除单份文档（删除前呈现将删除的块数并需确认）
- **提取质量与切分覆盖率** — 预览时告警 PDF 提取引入的编码噪声，并给出位置区间并集
  算出的覆盖率；两者的度量口径不同，不会互相顶替
- **知识库问答** — 基于已入库 chunk 的检索问答，答案带来源

相比第 8 章原示例，移除了与切分工作台重复的「一键加载文档」「查看文档切分」，
以及「学习笔记」「学习统计」两个页面。

改造的规格、设计与任务记录在 `openspec/` 下。

## 运行

需要 Python 3.10+，一个可连的 Qdrant 实例（默认 `http://localhost:6333`），
以及对话模型与嵌入模型。**所有服务商与密钥都来自 `.env`，本仓库不预设。**

```bash
pip install gradio python-dotenv     # hello_agents 按 Hello-Agents 章节说明安装
cp .env.example .env                 # 填入自己的密钥
python 11_Q&A_Assistant.py           # 打开 http://localhost:7860
```

`.env` 已在 `.gitignore` 中，不会被提交。

首次运行会在本地生成 `memory_data/`（学习记忆库），同样不入库。

### 命名空间

初始化时填的 `user_id` 决定读写哪个命名空间（`docs_<user_id>`）。问答、文档清单、
删除三处读操作与入库写入使用同一个命名空间——换了 `user_id` 就会看到一个空知识库，
这是预期行为，不是数据丢失。

## 文件

| 文件 | 说明 |
|---|---|
| `11_Q&A_Assistant.py` | 主应用，Gradio 界面与助手逻辑 |
| `chunking.py` | 三种切分策略的实现、清洗、入库与删除 |
| `chunk_viz.py` | 切分结果可视化 |
| `01`–`10_*.py` | 第 8 章原示例脚本，逐章演示 MemoryTool / RAGTool |
| `testconfig.py`、`testloadEmbeding.py` | 连通性自测脚本 |

## 许可

第 8 章示例代码来自 [datawhalechina/Hello-Agents](https://github.com/datawhalechina/Hello-Agents)，
采用 **CC BY-NC-SA 4.0**（署名—非商业性使用—相同方式共享），全文见 `LICENSE.txt`。

本仓库的改造成果同样以 CC BY-NC-SA 4.0 发布：**不得用于商业用途**，转载或再改造
需保留署名并以相同许可共享。
