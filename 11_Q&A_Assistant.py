#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能文档问答助手 - 基于HelloAgents的智能文档问答系统

这是一个完整的文档学习助手应用，支持：
- 加载多种格式文档并构建知识库（PDF、Word、Excel、PPT、图片、音频、文本等）
- 智能问答（基于RAG）
- 学习历程记录（基于Memory）
- 学习回顾和报告生成
"""

from dotenv import load_dotenv
load_dotenv(override=True)
import os
import time
import json
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from hello_agents.tools import MemoryTool, RAGTool
from hello_agents import HelloAgentsLLM
import gradio as gr

import chunk_viz as viz
import chunking as ck

def _escape_html(text: str) -> str:
    """转义HTML特殊字符"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


#: 参数面板的槽位数：取所有策略里参数个数的最大值。
#: 切换策略时只改这些槽位的可见性与取值范围，而不是重建控件——控件在构建期
#: 就已存在，所以回调的输入列表是固定的，不需要动态注册事件。
MAX_PARAM_SLOTS = max(len(ck.param_specs(name)) for name in ck.STRATEGIES)


#: 支持上传的扩展名。应用里只有「🔪 切分工作台」一处上传入口。
FILE_TYPES = [
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp",
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
    ".zip", ".tar", ".gz", ".rar",
    ".py", ".js", ".ts", ".java", ".cpp", ".c", ".h", ".css", ".scss",
]


def _uploaded_path(file: Any) -> str:
    """把 gr.File 的上传结果统一成文件路径字符串。"""
    if not file:
        return ""
    if isinstance(file, str):
        return file
    if isinstance(file, dict):
        return file.get("path") or file.get("name") or ""
    if isinstance(file, (list, tuple)) and file:
        return _uploaded_path(file[0])
    return str(file)


def _param_slot_updates(strategy: str):
    """按策略算出每个参数槽位的控件更新。

    返回 ``(滑块更新列表, 数字框更新列表)``，两者长度均为 MAX_PARAM_SLOTS。
    每个槽位同时备着一个滑块和一个数字框，``param_spec`` 的 ``kind`` 决定哪个
    可见——这样参数控件完全由注册表生成，加参数或改范围都只改 chunking.py。

    抽成模块级纯函数是为了能在没有浏览器的情况下断言：三种策略下展示的控件
    与其 ``param_spec`` 一致。
    """
    specs = ck.param_specs(strategy)
    sliders: List[Any] = []
    numbers: List[Any] = []

    for i in range(MAX_PARAM_SLOTS):
        if i >= len(specs):
            # 该策略用不到这么多参数，槽位整体隐藏
            sliders.append(gr.update(visible=False))
            numbers.append(gr.update(visible=False))
            continue

        spec = specs[i]
        common = {
            "label": spec["label"],
            "info": spec.get("info", ""),
            "value": spec["default"],
            "minimum": spec.get("min"),
            "maximum": spec.get("max"),
            "step": spec.get("step"),
        }
        sliders.append(gr.update(**common, visible=spec["kind"] == "slider"))
        numbers.append(gr.update(**common, visible=spec["kind"] == "number"))

    return sliders, numbers


def _collect_params(strategy: str, values: Any) -> Dict[str, Any]:
    """把参数槽位的值按策略组装成参数字典。

    ``values`` 是 (滑块1, 数字1, 滑块2, 数字2, ...) 的扁平序列——与界面上
    槽位的排列顺序一致。按 ``kind`` 取对应的那个控件，因此隐藏控件的值不会
    被误取。
    """
    values = list(values or [])
    params: Dict[str, Any] = {}

    for i, spec in enumerate(ck.param_specs(strategy)):
        slot = values[2 * i: 2 * i + 2]
        raw = slot[0] if spec["kind"] == "slider" else slot[1]
        if not slot:
            raw = spec["default"]
        try:
            params[spec["name"]] = int(raw)
        except (TypeError, ValueError):
            # 输入框被清空时 Number 会给 None，退回默认值而不是让整个预览失败
            params[spec["name"]] = spec["default"]

    return params


class PDFLearningAssistant:
    """智能文档问答助手"""

    def __init__(self, user_id: str = "default_user"):
        """初始化学习助手

        Args:
            user_id: 用户ID，用于隔离不同用户的数据
        """
        self.user_id = user_id
        self.session_id = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        # 初始化工具
        self.memory_tool = MemoryTool(user_id=user_id)
        self.rag_tool = RAGTool(rag_namespace=f"docs_{user_id}")
        self.llm_client = HelloAgentsLLM()

        # 切分工作台的会话状态（预览结果与指纹）
        self.workbench = ck.ChunkingWorkbench(namespace=self.rag_tool.rag_namespace)

    def _rag(self, **params: Any):
        """所有 RAG 读操作统一带上本助手的 namespace。

        库的 ``RAGTool.run`` 对 namespace 取 ``parameters.get("namespace", "default")``，
        而本助手写入用的是 ``docs_{user_id}``。不显式传就会去读 ``default``——检索
        要么恒为空，要么串到别的用户已入库的文档上，而且不报错。所以读入口收敛到
        这一个函数，不再有直接调用 ``rag_tool.run`` 的地方。
        """
        params.setdefault("namespace", self.rag_tool.rag_namespace)
        return self.rag_tool.run(params)

    def namespace_chunk_count(self) -> int:
        """本助手命名空间下已入库的 chunk 数；读取失败返回 -1。

        初始化时显示它，是为了让「命名空间里什么都没有」当场显形——否则这种
        情形要等到提问失败才发现，看起来像是检索坏了。
        """
        try:
            from qdrant_client.http.models import FieldCondition, Filter, MatchValue

            store = ck.get_store(self.rag_tool)
            result = store.client.count(
                collection_name=store.collection_name,
                count_filter=Filter(
                    must=[
                        FieldCondition(
                            key="rag_namespace",
                            match=MatchValue(value=self.rag_tool.rag_namespace),
                        )
                    ]
                ),
                exact=True,
            )
            return int(getattr(result, "count", 0) or 0)
        except Exception:
            return -1

    def preview_chunks(
        self, file_path: str, strategy: str, params: Dict[str, Any]
    ) -> Tuple[str, str]:
        """预览切分结果：提取 -> 切分 -> 物化 -> 渲染。全程不写数据库。

        渲染用的是即将入库的那份 chunk 列表，所以预览与实际入库不可能不一致。

        Returns:
            ``(HTML, 状态文本)``
        """
        if not file_path:
            return "", "❌ 请先上传文件"

        try:
            self.workbench.preview(file_path, strategy, params)
        except ck.UnsupportedFormatError as e:
            return "", f"❌ 文件格式不支持：{e}"
        except ck.EmptyContentError as e:
            return "", f"❌ 文件内容为空：{e}"
        except ck.EmbeddingUnavailableError as e:
            return "", f"❌ 向量模型不可用：{e}"
        except ck.ChunkingError as e:
            return "", f"❌ 切分失败：{e}"
        except Exception as e:
            return "", f"❌ 预览失败：{type(e).__name__}: {e}"

        wb = self.workbench
        label = ck.STRATEGIES[strategy]["label"]
        coverage = ck.assess_coverage(wb.chunks, len(wb.markdown))
        html = viz.render_chunk_report(
            wb.chunks,
            diagnostics=wb.diagnostics,
            title="切分预览（尚未入库）",
            doc_name=os.path.basename(file_path),
            strategy_label=label,
            params=wb.params,
            param_specs=ck.param_specs(strategy),
            coverage=coverage,
            quality=ck.cached_quality_report(file_path),
        )

        # 状态串只在有问题时多说一句：合格且覆盖完整时保持原样，不拿「一切正常」
        # 去占位置。两行摘要与面板里的区块出自同一份报告，说法一致。
        status = (
            f"✅ 预览完成：{label}，共 {len(wb.materialized)} 个 chunk。"
            f"确认无误后点击「确认入库」写入知识库。"
        )
        notes = [
            viz.summarize_quality(ck.cached_quality_report(file_path)),
            viz.summarize_coverage(coverage),
        ]
        notes = [n for n in notes if n]
        if notes:
            status += "\n" + "\n".join(notes)
        return html, status

    def commit_preview(
        self, file_path: str, strategy: str, params: Dict[str, Any]
    ) -> str:
        """把预览过的切分结果提交入库。

        入库前先比对指纹：如果预览之后动过参数或换了文件，就拒绝入库并要求
        重新预览。否则入库的和看到的就不是一回事了，而这个方案的前提正是
        两者一致。

        Returns:
            状态文本
        """
        if not self.workbench.is_fresh(file_path, strategy, params):
            return (
                "❌ 尚未预览，或预览之后参数/文件已改动。请先点击「预览切分」，"
                "确认结果后再入库。"
            )

        try:
            store = ck.get_store(self.rag_tool)
            n_new, n_removed = self.workbench.commit(store)
        except ck.IndexingError as e:
            # 旧切分已清除但新内容没写进去，必须如实报告，不能报成功
            return f"❌ 入库未完成：{e}"
        except ck.ChunkingError as e:
            return f"❌ 入库失败：{e}"
        except Exception as e:
            return f"❌ 入库失败：{type(e).__name__}: {e}"

        replaced = f"，替换掉旧切分 {n_removed} 块" if n_removed else ""
        return f"✅ 已入库：{n_new} 个 chunk{replaced}。可以到「💬 基于知识库智能问答」提问了。"

    def _get_chunks_from_qdrant(self, file_path: str) -> List[Dict]:
        """从Qdrant向量数据库直接获取文档的所有chunks

        Args:
            file_path: 文档的完整文件路径

        Returns:
            List[Dict]: chunks列表，按文档位置排序
        """
        try:
            from qdrant_client.http.models import Filter, FieldCondition, MatchValue

            # 获取RAG pipeline的store
            pipeline = self.rag_tool._get_pipeline(self.rag_tool.rag_namespace)
            store = pipeline["store"]

            # 构建过滤条件：匹配source_path，且限定在本助手的 namespace 内。
            # 集合是所有命名空间共享的，只按 source_path 过滤会在同一路径被
            # 两个用户各自入库时读到对方的 chunk。
            scroll_filter = Filter(
                must=[
                    FieldCondition(key="source_path", match=MatchValue(value=file_path)),
                    FieldCondition(
                        key="rag_namespace",
                        match=MatchValue(value=self.rag_tool.rag_namespace),
                    ),
                ]
            )

            all_points = []
            offset = None

            # 使用scroll API分页获取所有匹配的chunks
            while True:
                points, next_offset = store.client.scroll(
                    collection_name=store.collection_name,
                    scroll_filter=scroll_filter,
                    limit=100,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )

                if points:
                    all_points.extend(points)

                if next_offset is None:
                    break
                offset = next_offset

            # 提取并格式化chunks
            chunks = []
            for point in all_points:
                payload = point.payload
                content = payload.get("content", "")
                chunks.append({
                    "id": str(point.id),
                    "content": content,
                    "heading_path": payload.get("heading_path", ""),
                    "start": payload.get("start", 0),
                    "end": payload.get("end", 0),
                    "chunk_index": payload.get("chunk_index", 0),
                    "lang": payload.get("lang", ""),
                    "char_count": len(content),
                    "source_path": payload.get("source_path", ""),
                    "doc_id": payload.get("doc_id", ""),
                    "format": payload.get("format", ""),
                    # 入库时写下的切分来源，回看时据此还原「这份文档当初是怎么切的」
                    "split_strategy": payload.get("split_strategy", ""),
                    "split_params": payload.get("split_params") or {},
                })

            # 按start位置排序
            chunks.sort(key=lambda x: x["start"])

            # 重新编号chunk_index
            for i, chunk in enumerate(chunks):
                chunk["chunk_index"] = i + 1

            return chunks

        except Exception as e:
            print(f"[ChunkViz] 从Qdrant获取chunks失败: {e}")
            import traceback
            traceback.print_exc()
            return []

    def _doc_split_strategy(self, chunks: List[Dict]) -> str:
        """从 chunk 元数据里取出这份文档入库时用的切分策略。取不到则返回空串。"""
        for chunk in chunks:
            strategy = chunk.get("split_strategy") or ""
            if strategy in ck.STRATEGIES:
                return strategy
        return ""

    def _doc_split_params(self, chunks: List[Dict]) -> Dict[str, Any]:
        """取出入库时的切分参数，缺失的键用默认值补齐，保证参数条能完整渲染。"""
        strategy = self._doc_split_strategy(chunks)
        if not strategy:
            return {}
        params = ck.default_params(strategy)
        for chunk in chunks:
            stored = chunk.get("split_params") or {}
            if stored:
                params.update(stored)
                break
        return params

    def _doc_split_source(self, chunks: List[Dict]) -> str:
        """这份文档入库时的切分来源，一句话：策略标签 + 参数（值带单位）。

        取不到就返回「未记录」，**不回落到默认策略**——存量 chunk 没有
        ``split_strategy`` 字段，替它们填一个默认值，等于把猜的东西写成事实
        （design.md D2；spec 的「未记录切分来源的存量内容如实呈现」）。
        单位取自 ``ck.param_specs`` 的 ``unit``，不在文案里写死：块大小按 token
        计量，标成字符是错的。
        """
        strategy = self._doc_split_strategy(chunks)
        if not strategy:
            return "未记录"

        specs = ck.param_specs(strategy)
        labels = {p["name"]: p["label"] for p in specs}
        units = {p["name"]: p.get("unit", "") for p in specs}
        shown = "、".join(
            f"{labels.get(name, name)}{value}{units.get(name, '')}"
            for name, value in self._doc_split_params(chunks).items()
        )
        return f"{ck.STRATEGIES[strategy]['label']} · {shown}"

    def ask(self, question: str, use_advanced_search: bool = True) -> str:
        """向文档提问

        Args:
            question: 用户问题
            use_advanced_search: 是否使用高级检索（MQE + HyDE）

        Returns:
            str: 答案
        """
        # 记录问题到工作记忆
        self.memory_tool.run({
            "action":"add",
            "content":f"提问: {question}",
            "memory_type":"working",
            "importance":0.6,
            "session_id":self.session_id
        })

        # 使用RAG检索答案（经 _rag 注入本助手的 namespace）
        answer = self._rag(
            action="ask",
            question=question,
            limit=5,
            enable_advanced_search=use_advanced_search,
            enable_mqe=use_advanced_search,
            enable_hyde=use_advanced_search,
        )

        # 记录到情景记忆
        self.memory_tool.run({
            "action":"add",
            "content":f"关于'{question}'的学习",
            "memory_type":"episodic",
            "importance":0.7,
            "event_type":"qa_interaction",
            "session_id":self.session_id
        })

        return answer

    def chat_with_llm(self, message: str, history: List) -> str:
        """与大模型自由对话(不依赖知识库)
        
        Args:
            message: 用户消息
            history: 对话历史列表
            
        Returns:
            str: 大模型回复
        """
        try:
            # 构建消息列表
            messages = [
                {
                    "role": "system",
                    "content": """你是一个智能助手，拥有广泛的知识。请帮助用户回答问题，提供准确、详细的解答。

回答要求：
1. 清晰、有条理
2. 如果有代码示例，使用代码块
3. 如果不确定，诚实地告知用户
4. 用中文回答
"""
                }
            ]
            
            # 添加最近的10条历史对话作为上下文
            if history:
                for item in history[-10:]:
                    if isinstance(item, dict):
                        messages.append({
                            "role": item.get("role", "user"),
                            "content": item.get("content", "")
                        })
                    elif isinstance(item, (list, tuple)) and len(item) == 2:
                        messages.append({"role": "user", "content": item[0]})
                        messages.append({"role": "assistant", "content": item[1]})
            
            # 添加当前用户问题
            messages.append({"role": "user", "content": message})
            
            # 调用LLM生成回答(使用非流式调用)
            response = self.llm_client.invoke(messages)
            
            return response if response else "抱歉，我暂时无法回答这个问题。"
            
        except Exception as e:
            return f"大模型调用失败：{str(e)}\n\n请检查.env配置是否正确。"

    def _stored_documents(self) -> List[Dict[str, Any]]:
        """枚举本命名空间已入库的文档，并逐份量出质量、偏移可信度与切分来源。

        数据源是 Qdrant（``chunking.list_documents``），不是会话状态：知识库的
        内容是持久的，重启之后还在，而「知识库里现在有什么」恰恰是那一刻最需要
        看的东西。知识库文档页的表格与删除选择器都建在这个结果上，所以
        「列出知识库里有什么」只有这一个实现。

        单份文档度量失败（例如 chunk 结构异常）只跳过那一份，不让整张表消失。
        """
        try:
            store = ck.get_store(self.rag_tool)
            docs = ck.list_documents(store, self.rag_tool.rag_namespace)
        except Exception as e:
            print(f"[Docs] 枚举知识库文档失败: {e}")
            return []

        items: List[Dict[str, Any]] = []
        for d in docs:
            path = d["source_path"]
            try:
                chunks = self._get_chunks_from_qdrant(path)
                stored = ck.assess_stored_document(path, chunks)
            except Exception as e:
                print(f"[Docs] 度量失败 {os.path.basename(path)}: {e}")
                continue
            items.append({
                "source_path": path,
                "name": os.path.basename(path),
                "chunk_count": d["chunk_count"],
                "stored": stored,
                # 切分来源在这里一并算出来：chunks 已经在手里，两个只读字段的 helper
                # 不产生任何 I/O。存成一句话而不是把整份 chunks 挂进 item，是因为
                # 表格与选择器都只要这一句，没必要把每份文档的全部 chunk 留在内存里。
                "split_source": self._doc_split_source(chunks),
            })
        return sorted(items, key=lambda x: x["name"])

    def documents_table(self) -> str:
        """知识库中文档的逐份明细，渲染成一张 Markdown 表格。

        这张表是知识库文档页的清单本体：数据来自知识库本身，所以重启之后不加载
        任何文档也看得到库里的全部内容。列表换行会截断 Markdown 的列表结构，
        所以这里用表格而不是 bullet。
        """
        items = self._stored_documents()
        if not items:
            return "（知识库中还没有文档）"

        lines = [
            "| 文档 | Chunk 数 | 切分来源 | 私用区 | 全角拉丁 | 空壳表格率 | 偏移校验 | 判定 |",
            "| --- | ---: | --- | ---: | ---: | ---: | :---: | --- |",
        ]
        for item in items:
            stored = item["stored"]
            q = stored["quality"]
            position = "✅" if stored["position_trustworthy"] else "❌"
            verdict = "⚠️ 含噪声" if q["flagged"] else "✅ 正常"
            lines.append(
                f"| {item['name']} | {item['chunk_count']} | {item['split_source']} "
                f"| {q['pua_ratio']:.2%} | {q['fullwidth_ratio']:.2%} "
                f"| {q['hollow_table_ratio']:.1%} | {position} | {verdict} |"
            )
        lines.append("")
        lines.append(
            "「切分来源」= 该文档入库时用的切分策略与参数；**「未记录」表示入库时没有"
            "写下来源**（本次改造之前的存量内容），不是默认策略。"
        )
        lines.append("")
        lines.append(
            "「偏移校验」= 库中 chunk 的 `start`/`end` 能否对回原文，"
            "❌ 表示这份内容的覆盖率不可测（早期脚本入库的存量内容）。"
        )
        return "\n".join(lines)

    def document_chunk_count(self, source_path: str) -> int:
        """本命名空间下某一份文档的 chunk 数；读取失败返回 -1。"""
        try:
            store = ck.get_store(self.rag_tool)
            return ck.count_document_chunks(
                store, source_path, self.rag_tool.rag_namespace
            )
        except Exception:
            return -1

    def delete_choices(self) -> List[Tuple[str, str]]:
        """删除选择器的 ``(标签, 取值)`` 列表。

        取值是 ``source_path``：文档的身份就是它，而库里多数 ``source_path`` 是
        已经消失的 Gradio 临时路径，两份都叫「论文.pdf」时只有路径能分辨（design.md
        / Risks 第 3 条）。标签里带上块数与质量告警，是因为删除不可撤销，使用者
        需要在下决心之前看到这份东西是什么状态；质量合格时不加任何字
        （``viz.quality_label`` 返回空串，见「质量合格时不打扰」）。
        """
        choices: List[Tuple[str, str]] = []
        for item in self._stored_documents():
            parts = [
                item["name"],
                f"{item['chunk_count']} 块",
                viz.quality_label(item["stored"]["quality"]),
            ]
            choices.append((" · ".join(p for p in parts if p), item["source_path"]))
        return choices

    def delete_document(self, source_path: str) -> str:
        """删除知识库中一份已入库文档的全部 chunk。

        三步一步都不能省（design.md D3）：

        1. 删之前核对该 ``source_path`` 仍在 ``ck.list_documents`` 的结果里——
           ``purge_document`` 在文档已不在库里时返回空列表且不报错，直接报
           「已删除 0 块」就是假成功；
        2. 删除只走 ``ck.purge_document``——它按 ``source_path`` + ``rag_namespace``
           双条件过滤（``chunking._document_point_ids``），自己拼条件会删掉别的
           命名空间里同路径的数据；
        3. 删之后回读 ``count_document_chunks`` 核对归零，对不上就报失败。

        删除是实时生效的，不需要重启：检索每次都是现查 Qdrant，进程内没有文档
        索引，删除带 ``wait=True``（design.md D11）。

        Returns:
            给使用者看的状态文本
        """
        if not source_path:
            return "❌ 未选择文档，未删除任何内容。"

        namespace = self.rag_tool.rag_namespace
        try:
            store = ck.get_store(self.rag_tool)
            docs = ck.list_documents(store, namespace)
        except Exception as e:
            return f"❌ 无法读取知识库文档列表，未删除任何内容：{type(e).__name__}: {e}"

        matched = next((d for d in docs if d["source_path"] == source_path), None)
        if matched is None:
            return (
                "❌ 该文档已不在知识库中，未执行删除"
                "（列表可能已经过期，请点「刷新列表」后再试）。\n"
                f"文档路径：{source_path}"
            )

        before = matched["chunk_count"]
        try:
            ck.purge_document(store, source_path, namespace)
        except Exception as e:
            return f"❌ 删除失败，知识库未改动：{type(e).__name__}: {e}"

        left = ck.count_document_chunks(store, source_path, namespace)
        if left:
            return (
                f"❌ 删除未完成：应删除 {before} 块，知识库里仍剩 {left} 块。\n"
                f"文档路径：{source_path}"
            )

        return (
            f"✅ 已删除《{os.path.basename(source_path)}》的 {before} 个 chunk，"
            f"此后检索不再返回它的内容（不需要重启）。\n"
            f"文档路径：{source_path}"
        )


def create_gradio_ui():
    """创建Gradio Web UI"""
    # 全局助手实例
    assistant_state = {"assistant": None}

    def init_assistant(user_id: str) -> str:
        """初始化助手"""
        if not user_id:
            user_id = "web_user"
        assistant = PDFLearningAssistant(user_id=user_id)
        assistant_state["assistant"] = assistant

        # 把实际读写用的命名空间和其中的 chunk 数一并报出来：读写的命名空间一旦
        # 对不上，表现是「提问检索不到」，而不是报错，所以这里要让它可见。
        namespace = assistant.rag_tool.rag_namespace
        count = assistant.namespace_chunk_count()
        head = f"✅ 助手已初始化 (用户: {user_id} | 命名空间: {namespace}"

        if count < 0:
            return f"{head})\n⚠️ 未能读取该命名空间的 chunk 数，请检查 Qdrant 是否可用"
        if count == 0:
            return (
                f"{head})\n"
                f"⚠️ 该命名空间下还没有任何 chunk，问答将检索不到内容。"
                f"若文档是用别的用户ID入库的，请改用那个ID重新初始化。"
            )
        return f"{head} | 已入库 {count} 块)"

    def chat(message: str, history: List) -> Tuple[str, List]:
        """聊天功能(基于知识库)"""
        if assistant_state["assistant"] is None:
            return "", history + [[message, "❌ 请先初始化助手并加载文档"]]

        if not message.strip():
            return "", history

        # 判断是技术问题还是回顾问题
        if any(keyword in message for keyword in ["之前", "学过", "回顾", "历史", "记得"]):
            # 回顾学习历程
            response = assistant_state["assistant"].recall(message)
            response = f" **学习回顾**\n\n{response}"
        else:
            # 技术问答
            response = assistant_state["assistant"].ask(message)
            response = f"💡 **回答**\n\n{response}"

        # 新版Gradio格式：使用字典而非列表
        new_history = history + [
            {"role": "user", "content": message},
            {"role": "assistant", "content": response}
        ]
        return "", new_history

    def chat_llm_free(message: str, history: List) -> Tuple[str, List]:
        """与大模型自由对话(不依赖知识库)"""
        if assistant_state["assistant"] is None:
            return "", history + [[message, "❌ 请先初始化助手"]]

        if not message.strip():
            return "", history

        # 直接调用大模型
        response = assistant_state["assistant"].chat_with_llm(message, history)
        response = f" **大模型回答**\n\n{response}"

        # 新版Gradio格式
        new_history = history + [
            {"role": "user", "content": message},
            {"role": "assistant", "content": response}
        ]
        return "", new_history

    def refresh_docs_ui() -> Tuple[str, Any]:
        """知识库文档页：刷新文档表格与删除选择器。"""
        if assistant_state["assistant"] is None:
            return "（尚未初始化助手：请先到「🏠 开始使用」）", gr.update()
        assistant = assistant_state["assistant"]
        return assistant.documents_table(), gr.update(
            choices=assistant.delete_choices(), value=None
        )

    def delete_document_ui(
        source_path: str, confirmed: bool
    ) -> Tuple[str, Any, Any, Any]:
        """知识库文档页：删除。

        ``confirmed`` 为假时只报告「将删除什么」并拒绝，**不调用** ``delete_document``
        ——删除不可撤销，而集合是所有 user_id 共用的。
        """
        empty = gr.update()
        if assistant_state["assistant"] is None:
            return "❌ 请先到「🏠 开始使用」初始化助手", empty, empty, empty
        if not source_path:
            return "❌ 请先选择要删除的文档", empty, empty, empty

        assistant = assistant_state["assistant"]
        if not confirmed:
            count = assistant.document_chunk_count(source_path)
            shown = "未能读到它的 chunk 数" if count < 0 else f"共 {count} 个 chunk"
            return (
                "❌ 未勾选确认，没有删除任何内容。\n"
                f"即将删除的是《{os.path.basename(source_path)}》{shown}，"
                "删除不可撤销——确认请勾选「我确认删除这份文档的全部 chunk」后再点一次。\n"
                f"文档路径：{source_path}",
                empty, empty, empty,
            )

        status = assistant.delete_document(source_path)
        # 删完刷新表格与选择器，并把确认框复位，免得下一次误删
        return (
            status,
            assistant.documents_table(),
            gr.update(choices=assistant.delete_choices(), value=None),
            gr.update(value=False),
        )

    def workbench_preview(file, strategy: str, *param_values) -> Tuple[str, str]:
        """切分工作台：预览。只读，不写知识库。"""
        if assistant_state["assistant"] is None:
            return "", "❌ 请先到「🏠 开始使用」初始化助手"
        file_path = _uploaded_path(file)
        if not file_path:
            return "", "❌ 请先上传文件"
        params = _collect_params(strategy, param_values)
        return assistant_state["assistant"].preview_chunks(file_path, strategy, params)

    def workbench_commit(file, strategy: str, *param_values) -> str:
        """切分工作台：确认入库。"""
        if assistant_state["assistant"] is None:
            return "❌ 请先到「🏠 开始使用」初始化助手"
        file_path = _uploaded_path(file)
        if not file_path:
            return "❌ 请先上传文件"
        params = _collect_params(strategy, param_values)
        return assistant_state["assistant"].commit_preview(file_path, strategy, params)

    def on_strategy_change(strategy: str):
        """切换策略：更新策略说明与参数面板。

        参数控件完全按 param_spec 生成，所以三种策略展示的控件必然与其规格
        一致——这里不做任何与策略相关的硬编码。
        """
        spec = ck.STRATEGIES[strategy]
        outputs: List[Any] = [f"**{spec['label']}**：{spec['description']}"]
        sliders, numbers = _param_slot_updates(strategy)
        for i in range(MAX_PARAM_SLOTS):
            outputs.extend([sliders[i], numbers[i]])
        return outputs

    # 创建Gradio界面
    with gr.Blocks() as demo:
        gr.Markdown("""
        # 📚 智能文档问答助手

       基于HelloAgents的智能文档问答系统,支持:
        - 🔪 切分工作台：上传文档、先看切分效果，满意了再入库（PDF、Word、Excel、PPT、图片、音频、文本等）
        - 📚 知识库文档：看知识库里有什么、切分来源与提取质量，并可删除
        - 💬 基于知识库智能问答(基于RAG)
        - 🧠 基于大模型自由问答
        """)

        with gr.Tab("🏠 开始使用"):
            with gr.Row():
                user_id_input = gr.Textbox(
                    label="用户ID",
                    placeholder="输入你的用户ID（可选，默认为web_user）",
                    value="web_user"
                )
                init_btn = gr.Button("初始化助手", variant="primary")

            init_output = gr.Textbox(label="初始化状态", interactive=False)
            init_btn.click(init_assistant, inputs=[user_id_input], outputs=[init_output])

        with gr.Tab("🔪 切分工作台"):
            gr.Markdown("""
            ### 先看效果，再决定怎么切

            上传文档 → 选切分方式 → 调参数 → **预览**（不写知识库）→ 满意了再**确认入库**。
            预览和入库用的是同一份切分结果，所以你看到的，就是入库的。
            """)

            with gr.Row():
                wb_file = gr.File(
                    label="上传文档",
                    file_types=FILE_TYPES,
                    type="filepath",
                    scale=3,
                )
                wb_strategy = gr.Dropdown(
                    label="切分方式",
                    choices=[(ck.STRATEGIES[k]["label"], k) for k in ck.STRATEGIES],
                    value=ck.DEFAULT_STRATEGY,
                    scale=2,
                )

            wb_desc = gr.Markdown()

            with gr.Row():
                wb_sliders = [gr.Slider(label="参数", visible=False) for _ in range(MAX_PARAM_SLOTS)]
                wb_numbers = [gr.Number(label="参数", visible=False) for _ in range(MAX_PARAM_SLOTS)]

            # 参数控件的输入顺序与槽位排列一致：滑块1, 数字1, 滑块2, 数字2, ...
            # 这样 _collect_params 按 kind 取对应控件时，下标才对得上。
            wb_param_inputs: List[Any] = []
            for i in range(MAX_PARAM_SLOTS):
                wb_param_inputs.extend([wb_sliders[i], wb_numbers[i]])
            wb_inputs = [wb_file, wb_strategy] + wb_param_inputs

            with gr.Row():
                wb_preview_btn = gr.Button("🔍 预览切分", variant="primary")
                wb_commit_btn = gr.Button("✅ 确认入库", variant="secondary")

            wb_status = gr.Textbox(label="状态", interactive=False)
            wb_output = gr.HTML(label="切分预览")

            # 策略切换：重排参数面板。用 change 而不是 release——下拉框没有
            # 拖动过程，选中即生效。
            wb_strategy.change(
                on_strategy_change,
                inputs=[wb_strategy],
                outputs=[wb_desc] + wb_param_inputs,
            )

            wb_preview_btn.click(
                workbench_preview, inputs=wb_inputs, outputs=[wb_output, wb_status]
            )
            wb_commit_btn.click(
                workbench_commit, inputs=wb_inputs, outputs=[wb_status]
            )

            # 参数调节后自动重新预览，但只在「松手」时触发：滑块用 release，
            # 数字框用 submit。拖动过程中不重算，语义分割才不会卡。
            for _slider in wb_sliders:
                _slider.release(
                    workbench_preview, inputs=wb_inputs, outputs=[wb_output, wb_status]
                )
            for _number in wb_numbers:
                _number.submit(
                    workbench_preview, inputs=wb_inputs, outputs=[wb_output, wb_status]
                )

            # 首次进入时按默认策略生成参数面板
            demo.load(
                on_strategy_change,
                inputs=[wb_strategy],
                outputs=[wb_desc] + wb_param_inputs,
            )

        with gr.Tab("💬 基于知识库智能问答"):
            gr.Markdown("###  向已加载的文档提问(优先检索知识库)")
            chatbot = gr.Chatbot(
                label="对话历史",
                height=400
            )
            with gr.Row():
                msg_input = gr.Textbox(
                    label="输入问题",
                    placeholder="例如：什么是Transformer？ 或 我之前学过什么？",
                    scale=4
                )
                send_btn = gr.Button("发送", variant="primary", scale=1)

            gr.Markdown("###  快速提问")
            with gr.Row():
                ex1_btn = gr.Button("什么是大语言模型？", variant="secondary", size="sm")
                ex2_btn = gr.Button("Transformer核心组件？", variant="secondary", size="sm")
                ex3_btn = gr.Button("如何训练LLM？", variant="secondary", size="sm")
                ex4_btn = gr.Button("我之前学过什么？", variant="secondary", size="sm")
                ex5_btn = gr.Button("回顾注意力机制", variant="secondary", size="sm")

            ex1_btn.click(lambda: "什么是大语言模型？", outputs=[msg_input])
            ex2_btn.click(lambda: "Transformer架构有哪些核心组件？", outputs=[msg_input])
            ex3_btn.click(lambda: "如何训练大语言模型？", outputs=[msg_input])
            ex4_btn.click(lambda: "我之前学过什么内容？", outputs=[msg_input])
            ex5_btn.click(lambda: "回顾一下关于注意力机制的学习", outputs=[msg_input])

            msg_input.submit(chat, inputs=[msg_input, chatbot], outputs=[msg_input, chatbot])
            send_btn.click(chat, inputs=[msg_input, chatbot], outputs=[msg_input, chatbot])

        with gr.Tab("🧠 基于大模型自由问答"):
            gr.Markdown("###  与大模型自由对话(不限知识库范围)")
            llm_chatbot = gr.Chatbot(
                label="对话历史",
                height=400
            )
            with gr.Row():
                llm_msg_input = gr.Textbox(
                    label="输入问题",
                    placeholder="可以问任何问题,不限于已加载的文档...",
                    scale=4
                )
                llm_send_btn = gr.Button("发送", variant="primary", scale=1)

            gr.Markdown("###  快速提问")
            with gr.Row():
                ex6_btn = gr.Button("写一首春天的诗", variant="secondary", size="sm")
                ex7_btn = gr.Button("量子计算原理", variant="secondary", size="sm")
                ex8_btn = gr.Button("推荐编程书籍", variant="secondary", size="sm")
                ex9_btn = gr.Button("Python快速排序", variant="secondary", size="sm")
                ex10_btn = gr.Button("什么是机器学习？", variant="secondary", size="sm")

            ex6_btn.click(lambda: "帮我写一首关于春天的诗", outputs=[llm_msg_input])
            ex7_btn.click(lambda: "解释一下量子计算的基本原理", outputs=[llm_msg_input])
            ex8_btn.click(lambda: "给我推荐几本编程入门书籍", outputs=[llm_msg_input])
            ex9_btn.click(lambda: "如何用Python实现快速排序？", outputs=[llm_msg_input])
            ex10_btn.click(lambda: "什么是机器学习？", outputs=[llm_msg_input])

            llm_msg_input.submit(chat_llm_free, inputs=[llm_msg_input, llm_chatbot], outputs=[llm_msg_input, llm_chatbot])
            llm_send_btn.click(chat_llm_free, inputs=[llm_msg_input, llm_chatbot], outputs=[llm_msg_input, llm_chatbot])
            
        with gr.Tab("📚 知识库文档"):
            gr.Markdown(
                "### 知识库里现在有什么，以及怎么删\n"
                "列表来自知识库本身，不需要先上传或加载文档。"
            )
            docs_btn = gr.Button("刷新列表", variant="primary")
            docs_table = gr.Markdown()

            gr.Markdown("### 删除文档")
            with gr.Row():
                del_selector = gr.Dropdown(
                    label="选择要删除的文档（取值是它的入库路径）",
                    choices=[],
                    interactive=True,
                    scale=3,
                )
                del_confirm = gr.Checkbox(
                    label="我确认删除这份文档的全部 chunk（不可撤销）",
                    value=False,
                    scale=2,
                )
            del_btn = gr.Button("🗑️ 删除文档", variant="stop")
            del_output = gr.Textbox(label="删除状态", interactive=False)

            docs_btn.click(refresh_docs_ui, outputs=[docs_table, del_selector])
            del_btn.click(
                delete_document_ui,
                inputs=[del_selector, del_confirm],
                outputs=[del_output, docs_table, del_selector, del_confirm],
            )

    return demo


def main():
    """主函数 - 启动Gradio Web UI"""
    print("\n" + "="*60)
    print("智能文档问答助手")
    print("="*60)
    print("正在启动Web界面...\n")

    demo = create_gradio_ui()
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=False,
        show_error=True,
        theme=gr.themes.Soft()
    )


if __name__ == "__main__":
    main()

