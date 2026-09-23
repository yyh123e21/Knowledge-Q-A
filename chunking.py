#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档切分：把 markdown 切成可以入库的 chunk。

这个模块单独成文件，而不是塞进 11_Q&A_Assistant.py，有两个原因：

1. 切分逻辑值得独立阅读和单独测试，不该和 Gradio 接线缠在一起；
2. 主文件已经很长了。

模块的中心思想是一条漏斗：**预览和入库消费同一份 chunk 列表**。
`split_document()` 产出 chunk，`materialize()` 把它变成可入库的 dict，
之后预览渲染和 `index_chunks()` 读的是同一个 list。预览与实际入库之间
不存在第二条计算路径，所以两者不可能不一致。
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from hello_agents.memory.embedding import get_text_embedder


# ===========================================================================
# 错误类型
# ===========================================================================

class ChunkingError(Exception):
    """切分相关的可预期失败，消息直接展示给使用者。"""


class UnsupportedFormatError(ChunkingError):
    """文件格式不被支持。"""


class EmptyContentError(ChunkingError):
    """文件里提取不出任何文本。"""


class EmbeddingUnavailableError(ChunkingError):
    """语义分割需要向量模型，但模型不可用。"""


class IndexingError(ChunkingError):
    """旧 chunk 已清理，但新 chunk 写入失败——该文档处于未完成入库状态。"""


# ===========================================================================
# 1. markdown 提取（带缓存）
# ===========================================================================

class ExtractRecord(NamedTuple):
    """一次提取的完整结果（design.md D6）。

    四项一起存，是为了让预览、入库、``is_fresh`` 三处拿到同一份记录且不重算，
    同时让告警能回答三件事：检出了什么（``raw_quality``）、清掉了多少
    （``clean_counts``）、什么没修（``clean_quality``）。
    """

    text: str                      # 清洗后的文本——切分与入库消费的就是它
    raw_text: str                  # 清洗前的文本，只用于校验存量 chunk 的偏移
    raw_quality: Dict[str, Any]    # 清洗前的度量；**判定用这份**（design.md D5）
    clean_quality: Dict[str, Any]  # 清洗后的度量，用于说明「什么没修」
    clean_counts: Dict[str, int]   # 三类改动的计数，即「清掉了多少」


#: 缓存键 -> 提取记录。键为 (绝对路径, mtime, 文件大小)。
_EXTRACT_CACHE: Dict[Tuple[str, float, int], ExtractRecord] = {}

#: 供验证用的计数器（见 tasks.md 1.2）。
_EXTRACT_STATS = {"convert_calls": 0, "cache_hits": 0}


def _supported_extensions() -> set:
    """支持的文件扩展名集合，优先问库，问不到就用一份等价的内置表。"""
    try:
        from hello_agents.memory.rag import pipeline as _p

        fn = getattr(_p, "_is_markitdown_supported_format", None)
        if callable(fn):
            # 库只提供了「路径 -> bool」的接口，这里用它反推集合没有意义，
            # 因此仍然使用下面的内置表，只是确认库在。
            pass
    except Exception:
        pass
    return {
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm",
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp",
        ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
        ".zip", ".tar", ".gz", ".rar",
        ".py", ".js", ".ts", ".java", ".cpp", ".c", ".h", ".css", ".scss",
        ".log", ".conf", ".ini", ".cfg", ".yaml", ".yml", ".toml",
    }


def _convert_via_library(path: str) -> str:
    """调用库的 markdown 转换函数，失败时回退到纯文本读取。

    ``_convert_to_markdown`` 是库的私有函数（下划线前缀），库升级有可能改名。
    所以这里做两层回退：私有转换函数 -> 私有纯文本读取 -> 自己读文件。
    这样库改名最多让提取质量下降，不会让整个切分工作台不可用。
    """
    try:
        from hello_agents.memory.rag import pipeline as _p
    except Exception as e:  # pragma: no cover - 环境缺库时才会走到
        print(f"[Chunking] 无法导入 hello_agents 的 pipeline: {e}")
        _p = None

    if _p is not None:
        convert = getattr(_p, "_convert_to_markdown", None)
        if callable(convert):
            try:
                text = convert(path)
                if isinstance(text, str) and text.strip():
                    return text
            except Exception as e:
                print(f"[Chunking] _convert_to_markdown 失败: {e}")

        fallback = getattr(_p, "_fallback_text_reader", None)
        if callable(fallback):
            try:
                text = fallback(path)
                if isinstance(text, str) and text.strip():
                    return text
            except Exception as e:
                print(f"[Chunking] _fallback_text_reader 失败: {e}")

    # 最后的兜底：自己按文本读一遍
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def _extract_record(path: str) -> ExtractRecord:
    """提取 -> 度量 -> 清洗，结果按 ``(路径, mtime, 大小)`` 缓存。

    清洗放在这条漏斗内部（design.md D1）：三个调用点（入库、预览、``is_fresh``）
    自动全部覆盖，且预览与入库消费的是同一份文本——「预览与入库一致」与「位置
    信息为真实偏移」两条既有契约都不需要改动。

    度量在清洗**之前**做：清洗后两项噪声都归零，闸门永远不会触发（design.md D5）。
    """
    if not os.path.exists(path):
        raise ChunkingError(f"文件不存在: {path}")

    ext = (os.path.splitext(path)[1] or "").lower()
    if ext not in _supported_extensions():
        raise UnsupportedFormatError(f"不支持的格式: {ext or '(无扩展名)'}")

    st = os.stat(path)
    key = (os.path.abspath(path), st.st_mtime, st.st_size)
    if key in _EXTRACT_CACHE:
        _EXTRACT_STATS["cache_hits"] += 1
        return _EXTRACT_CACHE[key]

    _EXTRACT_STATS["convert_calls"] += 1
    text = _convert_via_library(path)
    if not text or not text.strip():
        raise EmptyContentError("未能从文件中提取到任何文本内容")

    raw_quality = assess_extraction_quality(text)
    cleaned, counts = normalize_extracted_text(text)
    record = ExtractRecord(
        text=cleaned,
        raw_text=text,
        raw_quality=raw_quality,
        clean_quality=assess_extraction_quality(cleaned),
        clean_counts=counts,
    )

    _EXTRACT_CACHE[key] = record
    return record


def extract_markdown(path: str) -> str:
    """提取文档的 markdown 文本（已清洗），按 ``(路径, mtime, 大小)`` 缓存。

    PDF / 音频 / 图片的提取包含 OCR 与语音转写，是最贵的一步。缓存让使用者
    在策略之间来回切换、反复调参时不必重复付出这个代价。

    Raises:
        UnsupportedFormatError: 扩展名不在支持列表内。
        EmptyContentError: 提取结果为空。
    """
    return _extract_record(path).text


def _quality_report(record: ExtractRecord) -> Dict[str, Any]:
    """把提取记录摊成给界面用的报告。

    ``flagged`` 与 ``reasons`` 取自**清洗前**的度量——判定只看编码噪声，而清洗
    正是把噪声去掉的那一步，取清洗后就永远不会告警（design.md D5）。
    """
    raw = record.raw_quality
    clean = record.clean_quality
    return {
        "flagged": raw["flagged"],
        "reasons": list(raw["reasons"]),
        "counts": dict(record.clean_counts),
        "before": dict(raw),
        "after": dict(clean),
        "char_count": raw["char_count"],
        "clean_char_count": clean["char_count"],
    }


def cached_quality_report(path: str) -> Optional[Dict[str, Any]]:
    """取某路径**已缓存**的质量报告；没提取过就返回 ``None``，不抛异常。

    只读缓存、不触发提取：调用方要的是「这次提取检出了什么」，为此把文件重新
    提取一遍既慢又没必要。

    注意这份报告活在内存里，重启即空——所以回看路径**不用**它，改从已存 chunk
    的文本度量（design.md D12）。
    """
    try:
        if not os.path.exists(path):
            return None
        st = os.stat(path)
    except OSError:
        return None

    record = _EXTRACT_CACHE.get((os.path.abspath(path), st.st_mtime, st.st_size))
    return _quality_report(record) if record else None


def extraction_stats() -> Dict[str, int]:
    """返回提取缓存的命中/转换次数（验证用，见 tasks.md 1.2）。"""
    return dict(_EXTRACT_STATS)


def clear_extraction_cache() -> None:
    """清空提取缓存。"""
    _EXTRACT_CACHE.clear()


# ===========================================================================
# 1b. 提取质量：清洗与度量
# ===========================================================================

#: 三个私用区（PUA）。这些码位没有约定字形，PDF 文本层常拿它们内嵌私有字体的
#: 字形，落到提取结果里就是「词中间夹了一个匹配不上的字符」。
_PUA_RANGES: Tuple[Tuple[int, int], ...] = (
    (0xE000, 0xF8FF),      # BMP 私用区
    (0xF0000, 0xFFFFD),    # 补充私用区 A
    (0x100000, 0x10FFFD),  # 补充私用区 B
)

#: 全角数字与全角拉丁字母，各减 ``_FULLWIDTH_OFFSET`` 即得对应 ASCII。
#: 只转换这三个精确区间：``unicodedata.normalize("NFKC")`` 会把中文全角标点
#: （，！？：）一并转成 ASCII，还会做兼容分解（①→1），改动面远超需要（design.md D2）。
_FULLWIDTH_RANGES: Tuple[Tuple[int, int], ...] = (
    (0xFF10, 0xFF19),  # ０-９
    (0xFF21, 0xFF3A),  # Ａ-Ｚ
    (0xFF41, 0xFF5A),  # ａ-ｚ
)
_FULLWIDTH_OFFSET = 0xFEE0

#: 只折叠**两侧都是汉字**的空格：空格在拉丁文本、代码块与表格里是有意义的，
#: 全局折叠会把它们毁掉（design.md D4）。用零宽断言，使「汉 汉 汉」里的两个
#: 空格各按原串判定，一遍就能全部折叠，且结果幂等。
_HAN_SPACE_RE = re.compile(r"(?<=[一-鿿]) (?=[一-鿿])")

#: markdown 表格行：以 ``|`` 开头、以 ``|`` 结尾（允许前导空白）。
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")

#: 判定阈值。实测问题件 PUA 4.08% / 全角拉丁 10.31%，健康文档一律 0.00%，
#: 两侧各有约 40 倍间隔，阈值取在靠近健康一侧（design.md D5）。集中定义在这里，
#: 便于按语料调整；误报时报告里带各指标原值，可直接看数而不是猜。
QUALITY_PUA_RATIO_LIMIT = 0.001        # 0.1%
QUALITY_FULLWIDTH_RATIO_LIMIT = 0.005  # 0.5%

#: 覆盖率低于此值即认为切分漏了内容。实测六份文档的并集覆盖率是 99.67%–100.00%，
#: 最低的一份只留 0.67pp 余量，样本量小——所以报告里同时给原始覆盖率（design.md D9）。
COVERAGE_RATIO_LIMIT = 0.99


def _is_pua(code: int) -> bool:
    """码位是否落在三个私用区内。"""
    return any(lo <= code <= hi for lo, hi in _PUA_RANGES)


def _halfwidth_of(code: int) -> Optional[int]:
    """全角码位转半角；不在三个区间内返回 ``None``（即这个字符不动）。"""
    for lo, hi in _FULLWIDTH_RANGES:
        if lo <= code <= hi:
            return code - _FULLWIDTH_OFFSET
    return None


def normalize_extracted_text(text: str) -> Tuple[str, Dict[str, int]]:
    """清洗提取文本，返回 ``(清洗后文本, 各类改动计数)``。

    三类定点改动：删私用区字符、全角拉丁与全角数字转半角、折叠汉字之间的空格。
    除此之外不动任何字符——中文标点逐字保留。对同一输入结果完全确定。

    分两步而不是一遍扫完：折叠空格必须看到**删掉私用区之后**的相邻关系，否则
    ``汉 <PUA> 汉`` 这类要等 PUA 消失才成立的相邻会被漏掉，两遍结果还不一致，
    幂等性就不成立。
    """
    kept: List[str] = []
    pua_removed = 0
    fullwidth_folded = 0

    for ch in text:
        code = ord(ch)
        if _is_pua(code):
            pua_removed += 1
            continue
        half = _halfwidth_of(code)
        if half is not None:
            fullwidth_folded += 1
            kept.append(chr(half))
            continue
        kept.append(ch)

    folded = "".join(kept)
    han_space_folded = len(_HAN_SPACE_RE.findall(folded))
    folded = _HAN_SPACE_RE.sub("", folded)

    return folded, {
        "pua_removed": pua_removed,
        "fullwidth_folded": fullwidth_folded,
        "han_space_folded": han_space_folded,
    }


def _table_metrics(text: str) -> Tuple[float, float]:
    """返回 ``(表内字符占比, 空壳表格率)``。

    空壳表格率 = 表格行里的空单元格占全部单元格的比例。提取器会把表格压成只剩
    边框的「｜ ｜ ｜」行，这个比例就会很高。两项都只呈现、不判定（design.md D5）。
    """
    table_chars = 0
    cells_total = 0
    cells_empty = 0

    for line in text.splitlines():
        if not _TABLE_ROW_RE.match(line):
            continue
        table_chars += len(line)
        for cell in line.strip().strip("|").split("|"):
            cells_total += 1
            if not cell.strip():
                cells_empty += 1

    total = len(text)
    return (
        table_chars / total if total else 0.0,
        cells_empty / cells_total if cells_total else 0.0,
    )


def assess_extraction_quality(text: str) -> Dict[str, Any]:
    """度量提取文本的质量，返回各指标原值与判定结果。

    度量对象是**提取文本**，不是 chunk：提取质量是「提取」的性质，不该随使用者
    调切分参数而变（design.md D11）。

    判定只用编码噪声两项——实测它们能把问题件与健康文档完全分开（4.08% / 10.31%
    对 0.00% / 0.00%），而表格指标与 CJK 占比不能，拿它们做闸门只会误报表格密集
    的健康文档（design.md D5）。不能判定的指标照样返回，供界面呈现。

    **必须在清洗前调用**：清洗之后两项噪声都归零，闸门永远不会触发。
    """
    total = len(text)
    if total == 0:
        return {
            "char_count": 0,
            "pua_ratio": 0.0,
            "fullwidth_ratio": 0.0,
            "cjk_ratio": 0.0,
            "table_char_ratio": 0.0,
            "hollow_table_ratio": 0.0,
            "flagged": False,
            "reasons": [],
        }

    pua = sum(1 for ch in text if _is_pua(ord(ch)))
    fullwidth = sum(1 for ch in text if _halfwidth_of(ord(ch)) is not None)
    cjk = sum(1 for ch in text if _is_cjk(ch))
    table_char_ratio, hollow_ratio = _table_metrics(text)

    pua_ratio = pua / total
    fullwidth_ratio = fullwidth / total

    reasons: List[str] = []
    if pua_ratio > QUALITY_PUA_RATIO_LIMIT:
        reasons.append(
            f"私用区字符 {pua_ratio:.2%}（阈值 {QUALITY_PUA_RATIO_LIMIT:.2%}）"
        )
    if fullwidth_ratio > QUALITY_FULLWIDTH_RATIO_LIMIT:
        reasons.append(
            f"全角拉丁与全角数字 {fullwidth_ratio:.2%}"
            f"（阈值 {QUALITY_FULLWIDTH_RATIO_LIMIT:.2%}）"
        )

    return {
        "char_count": total,
        "pua_ratio": pua_ratio,
        "fullwidth_ratio": fullwidth_ratio,
        "cjk_ratio": cjk / total,
        "table_char_ratio": table_char_ratio,
        "hollow_table_ratio": hollow_ratio,
        "flagged": bool(reasons),
        "reasons": reasons,
    }


def covered_chars(chunks: Sequence[Dict[str, Any]]) -> Optional[int]:
    """各 chunk 位置区间的并集长度，即被切分覆盖到的字符数。

    用区间并集而不是「各 chunk 字符数之和」：相邻 chunk 的重叠会让后者把同一段
    文字数两遍，于是那个和可以超过文本总长（实测 99.67%–114.72%），既读不出
    「切分有没有漏内容」，也容易被当成算错。并集在结构上不可能超过文本总长。

    没有任何可用的起止位置时返回 ``None``——调用方据此不出这个数，而不是编一个。
    """
    spans = sorted(
        (c["start"], c["end"])
        for c in chunks
        if isinstance(c.get("start"), int)
        and isinstance(c.get("end"), int)
        and c["end"] > c["start"]
    )
    if not spans:
        return None

    covered = 0
    cur_start, cur_end = spans[0]
    for start, end in spans[1:]:
        if start > cur_end:  # 与前一段不相接，结算前一段
            covered += cur_end - cur_start
            cur_start, cur_end = start, end
        else:  # 重叠或首尾相接，向后延展
            cur_end = max(cur_end, end)
    covered += cur_end - cur_start
    return covered


def assess_coverage(
    chunks: Sequence[Dict[str, Any]], text_len: int
) -> Dict[str, Any]:
    """度量本次切分的完整性，返回覆盖率、冗余与是否低于阈值。

    覆盖率 = 位置区间并集 ÷ 提取文本长度，结构上 ≤ 100%，回答「切分有没有整段
    漏掉内容」。冗余 = Σchunk ÷ 并集 − 1，单独报出，让 ``chunk_overlap`` 的实际
    代价可见——两者必须分开：混进一个数里会得出「重叠越多内容越完整」这种误导
    结论（design.md D9）。

    分母只能是提取后的文本长度：PDF、Word、PPT 是二进制容器，没有「原始文档
    字符数」这个东西（design.md D10）。因此覆盖率衡量的是**切分**是否完整，
    提取阶段丢掉的内容不在这个分母里，它看不见。

    ``start`` / ``end`` 缺失或区间不合法时不抛异常，返回
    ``position_available=False`` 与 ``None`` 覆盖率——存量 ``default`` 命名空间的
    偏移不可信，回看路径靠这个标记决定不出数（design.md D12）。
    """
    total_chunk_chars = sum(len(c.get("content") or "") for c in chunks)
    covered = covered_chars(chunks)

    result: Dict[str, Any] = {
        "text_len": text_len,
        "chunk_count": len(chunks),
        "total_chunk_chars": total_chunk_chars,
        "covered_chars": covered,
        "coverage_ratio": None,
        "redundancy_ratio": None,
        "position_available": covered is not None,
        "below_threshold": False,
    }
    if covered is None or text_len <= 0:
        return result

    coverage = min(covered / text_len, 1.0)
    result["coverage_ratio"] = coverage
    result["redundancy_ratio"] = total_chunk_chars / covered - 1 if covered else None
    result["below_threshold"] = coverage < COVERAGE_RATIO_LIMIT
    return result


# ===========================================================================
# 2. token 计数
# ===========================================================================

def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x20000 <= code <= 0x2A6DF
        or 0x2A700 <= code <= 0x2B73F
        or 0x2B740 <= code <= 0x2B81F
        or 0x2B820 <= code <= 0x2CEAF
        or 0xF900 <= code <= 0xFAFF
    )


def approx_token_len(text: str) -> int:
    """近似 token 计数，口径与库完全一致：CJK 按字、非 CJK 按空白分词。

    直接转发库函数，这样界面上的 ``chunk_size`` 与库里那个参数含义相同——
    这是本次变更要修的单位错标的落点：它一直是 token，不是字符。
    """
    try:
        from hello_agents.memory.rag import pipeline as _p

        fn = getattr(_p, "_approx_token_len", None)
        if callable(fn):
            return fn(text)
    except Exception:
        pass
    cjk = sum(1 for ch in text if _is_cjk(ch))
    return cjk + len([t for t in text.split() if t])


# ===========================================================================
# 3. 段落/标题扫描：偏移一律取原文真实位置
# ===========================================================================

#: 围栏代码块的起止标记（``` 或 ~~~）。
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _scan_lines(text: str):
    """逐行扫描，产出 ``(偏移, 原始行, 是否处于围栏代码块内)``。

    围栏状态必须显式跟踪：代码块里的 ``# 注释`` 也是以 ``#`` 开头的，
    不区分就会把代码注释当成标题解析，进而污染标题路径。
    """
    in_fence = False
    marker: Optional[str] = None
    pos = 0

    for raw in text.splitlines(keepends=True):
        m = _FENCE_RE.match(raw)
        if m:
            if not in_fence:
                in_fence, marker = True, m.group(1)
            elif m.group(1) == marker:
                in_fence, marker = False, None
            yield pos, raw, True
            pos += len(raw)
            continue

        yield pos, raw, in_fence
        pos += len(raw)


def _iter_blocks_with_headings(text: str) -> List[Dict[str, Any]]:
    """按标题与空行切出块，每块带原文真实偏移与所属标题路径。

    与库的 ``_split_paragraphs_with_headings`` 的关键区别：库用
    ``end_pos - len(content)`` 反推起点，正文被 ``.strip()`` 过之后这个
    推算就不再精确。这里逐行累计真实位置，所以 ``text[start:end]`` 就是
    这一块的原文，偏移永远是真的。

    偏移准不准很要紧：库的 ``compute_graph_signals_from_pool`` 用 1600 字符
    的近邻窗口给同文档 chunk 加权，``expand_neighbors_from_pool`` 也靠它找
    邻居。偏移错了不会报错，只会让检索排序悄悄变差。
    """
    blocks: List[Dict[str, Any]] = []
    heading_stack: List[str] = []

    buf_start: Optional[int] = None
    buf_end: Optional[int] = None
    #: 已读入标题栈、但还没被任何块消费的标题行起始位置
    pending_heading_start: Optional[int] = None

    def flush() -> None:
        nonlocal buf_start, buf_end, pending_heading_start
        if buf_start is None:
            # 没有待发内容。标题继续挂起，等正文来了再一起成块——否则
            # 「标题 + 空行 + 正文」这种最常见的写法会把标题丢掉。
            return
        # 收紧尾部空白，同时把 end 一起收，保持 text[start:end] == content
        content = text[buf_start:buf_end].rstrip()
        if content:
            blocks.append({
                "start": buf_start,
                "end": buf_start + len(content),
                "content": content,
                "heading_path": " > ".join(heading_stack) if heading_stack else None,
            })
        buf_start = None
        buf_end = None
        pending_heading_start = None

    for pos, raw, in_fence in _scan_lines(text):
        stripped = raw.strip()

        if not in_fence and stripped.startswith("#"):
            flush()
            m = re.match(r"^\s*(#+)", raw)
            level = len(m.group(1)) if m else 1
            title = raw.strip().lstrip("#").strip()
            if level <= len(heading_stack):
                heading_stack = heading_stack[: level - 1]
            heading_stack.append(title)
            # 标题行本身也要进正文。章节标题往往是整块里最容易被检索命中的
            # 一段文字，只把它记进 heading_path 而把文字丢掉，这块内容就再也
            # 匹配不到「关于某一章」的提问了。连续多个标题时取最外层那个，
            # 这样整段标题文字都不会漏。
            if pending_heading_start is None:
                pending_heading_start = pos
            continue

        if not in_fence and stripped == "":
            flush()
            continue

        if buf_start is None:
            buf_start = pending_heading_start if pending_heading_start is not None else pos
        # 不含行尾换行，让块内容贴着原文
        buf_end = max(buf_end or 0, pos + len(raw.rstrip("\n")))

    flush()

    # 结尾只有标题、后面没有正文时，标题同样不该被丢掉
    if pending_heading_start is not None:
        buf_start = pending_heading_start
        buf_end = pending_heading_start + len(text[pending_heading_start:].rstrip("\n"))
        flush()

    return blocks


def _heading_index(text: str) -> List[Tuple[int, str]]:
    """扫描 markdown，返回 ``[(偏移, 标题路径)]``，用于给任意位置定位所属标题。"""
    headings: List[Tuple[int, str]] = []
    stack: List[str] = []
    for pos, raw, in_fence in _scan_lines(text):
        if in_fence:
            continue
        if raw.strip().startswith("#"):
            m = re.match(r"^\s*(#+)", raw)
            if m:
                level = len(m.group(1))
                title = raw.strip().lstrip("#").strip()
                if level <= len(stack):
                    stack = stack[: level - 1]
                stack.append(title)
                headings.append((pos, " > ".join(stack)))
    return headings


def _heading_at(headings: Sequence[Tuple[int, str]], pos: int) -> Optional[str]:
    """返回偏移 ``pos`` 所处的标题路径。"""
    current: Optional[str] = None
    for start, path in headings:
        if start <= pos:
            current = path
        else:
            break
    return current


# ===========================================================================
# 4. 超长单元兜底
# ===========================================================================

_SENTENCE_END = "。！？；!?;\n"


def _iter_sentences(text: str, start: int, end: int) -> List[Tuple[int, int]]:
    """在 ``[start, end)`` 内按句末标点切句，保留真实偏移。"""
    out: List[Tuple[int, int]] = []
    seg_start = start
    i = start
    while i < end:
        if text[i] in _SENTENCE_END:
            out.append((seg_start, i + 1))
            seg_start = i + 1
        i += 1
    if seg_start < end:
        out.append((seg_start, end))
    return out


def _hard_split(
    text: str,
    start: int,
    end: int,
    budget: int,
    counter: Callable[[str], int],
) -> List[Tuple[int, int]]:
    """按步长硬拆——单句仍超预算时的最后手段（例如 OCR 出的无标点长文）。

    步长不能直接用预算：预算按 token 计，而 token 数与字符数并不相等
    （CJK 一字一 token，英文一个词一 token）。所以先按预算估一个步长，再用
    真实计数二分收敛到「不超过预算的最大步长」。少了这一步，产出的块会比
    上限略微超出——对纯 CJK 文本正好差一个 token。
    """
    out: List[Tuple[int, int]] = []
    pos = start
    while pos < end:
        remaining = end - pos
        step = min(budget, remaining)
        counted = counter(text[pos:pos + step]) or 1
        # 以首次实测的密度外推一个上界，再在 [下界, 上界] 里二分
        if counted <= budget:
            lo, hi = step, min(remaining, step * budget // counted + 1)
        else:
            lo, hi = 1, step
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if counter(text[pos:pos + mid]) <= budget:
                lo = mid
            else:
                hi = mid - 1
        step = max(1, lo)
        out.append((pos, pos + step))
        pos += step
    return out


def _split_oversized(
    text: str,
    start: int,
    end: int,
    budget: int,
    counter: Callable[[str], int],
) -> List[Tuple[int, int]]:
    """把超预算的区间按句末标点继续拆，仍超预算时硬拆。

    没有这层兜底，一个没有空行的长页面会产出上万字的单个 chunk：既撑爆
    上下文，又因为向量被整段稀释而检索不到。
    """
    if counter(text[start:end]) <= budget:
        return [(start, end)]

    out: List[Tuple[int, int]] = []
    cur_start: Optional[int] = None
    cur_end = start
    cur_len = 0

    for s, e in _iter_sentences(text, start, end):
        seg_len = counter(text[s:e]) or 1
        if seg_len > budget:
            if cur_start is not None:
                out.append((cur_start, cur_end))
                cur_start, cur_len = None, 0
            out.extend(_hard_split(text, s, e, budget, counter))
            continue
        if cur_start is None:
            cur_start, cur_end, cur_len = s, e, seg_len
        elif cur_len + seg_len <= budget:
            cur_end, cur_len = e, cur_len + seg_len
        else:
            out.append((cur_start, cur_end))
            cur_start, cur_end, cur_len = s, e, seg_len

    if cur_start is not None:
        out.append((cur_start, cur_end))
    return out


# ===========================================================================
# 5. 装填：把单元聚合成 chunk
# ===========================================================================

def _pack_units(
    units: List[Dict[str, Any]],
    text: str,
    max_tokens: int,
    overlap_tokens: int,
    budget: Optional[int] = None,
    counter: Optional[Callable[[str], int]] = None,
) -> List[Dict[str, Any]]:
    """贪心装填：按预算装单元，装满就切一块，并按预算回退保留重叠。

    chunk 的 ``content`` 直接取 ``text[start:end]``，所以
    ``text[start:end] == content`` 恒成立，偏移永远是真的。

    重叠按整单元回退（而不是按字符切一半），保证重叠部分也是完整的句子。
    """
    counter = counter or approx_token_len
    budget = budget if budget is not None else max_tokens

    chunks: List[Dict[str, Any]] = []
    cur: List[Dict[str, Any]] = []
    cur_len = 0

    for u in units:
        u_len = counter(text[u["start"]:u["end"]]) or 1
        if cur_len + u_len <= budget or not cur:
            cur.append(u)
            cur_len += u_len
            continue

        chunks.append(_emit(cur, text))
        cur, cur_len = _overlap_tail(cur, text, overlap_tokens, counter)
        # 触发换块的这一个单元必须成为新块的第一员。漏掉它，每个单元都会
        # 被下一次换块顶掉，文档被静默丢内容——而偏移依然自洽，所以
        # 「markdown[start:end] == content」这条断言发现不了。
        #
        # 补上它之后必须复查预算：重叠是尽力而为，上限是硬约束。带着重叠
        # 装不下这个单元时，这一处边界就不带重叠，直接从新单元重新开始。
        # 单元都已预拆到 <= budget，所以新块必定不超限；少了这一次复查，
        # 重叠会把块顶出上限（默认参数下 800 能变成 822）。
        if cur_len + u_len > budget:
            cur, cur_len = [], 0
        cur.append(u)
        cur_len += u_len

    if cur:
        chunks.append(_emit(cur, text))
    return chunks


def _emit(units: List[Dict[str, Any]], text: str) -> Dict[str, Any]:
    """把一组单元合成一个 chunk，偏移取首尾单元的真实位置。"""
    start = units[0]["start"]
    end = units[-1]["end"]
    heading_path = next(
        (u.get("heading_path") for u in reversed(units) if u.get("heading_path")),
        None,
    )
    return {
        "content": text[start:end],
        "start": start,
        "end": end,
        "heading_path": heading_path,
    }


def _overlap_tail(
    units: List[Dict[str, Any]],
    text: str,
    overlap_tokens: int,
    counter: Callable[[str], int],
) -> Tuple[List[Dict[str, Any]], int]:
    """保留尾部若干单元作为下一块的重叠部分。"""
    if overlap_tokens <= 0:
        return [], 0
    kept: List[Dict[str, Any]] = []
    kept_len = 0
    for u in reversed(units):
        u_len = counter(text[u["start"]:u["end"]]) or 1
        if kept_len + u_len > overlap_tokens:
            break
        kept.append(u)
        kept_len += u_len
    kept.reverse()
    return kept, kept_len


# ===========================================================================
# 6. 三种切分策略
# ===========================================================================

def split_paragraph(
    text: str, params: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """段落分割：以空行段落为单元聚合。

    标题行在这里只是普通文本，不参与结构判断——想要按标题切请用
    Markdown 结构感知策略。
    """
    max_tokens = int(params.get("chunk_size", 800))
    overlap = int(params.get("chunk_overlap", 80))

    blocks = _iter_blocks_with_headings(text)
    if not blocks:
        return [], {}

    units: List[Dict[str, Any]] = []
    for b in blocks:
        if approx_token_len(b["content"]) > max_tokens:
            for s, e in _split_oversized(text, b["start"], b["end"], max_tokens, approx_token_len):
                units.append({"start": s, "end": e, "heading_path": None})
        else:
            units.append({"start": b["start"], "end": b["end"], "heading_path": None})

    return _pack_units(units, text, max_tokens, overlap), {}


def split_markdown(
    text: str, params: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Markdown 结构感知：按标题层级与空行段落切分，记录标题路径。

    这是「🏠 开始使用」一键加载所用的默认策略，也是与库原有行为最接近的一种。
    """
    max_tokens = int(params.get("chunk_size", 800))
    overlap = int(params.get("chunk_overlap", 80))

    blocks = _iter_blocks_with_headings(text)
    if not blocks:
        return [], {}

    units: List[Dict[str, Any]] = []
    for b in blocks:
        if approx_token_len(b["content"]) > max_tokens:
            for s, e in _split_oversized(text, b["start"], b["end"], max_tokens, approx_token_len):
                units.append({"start": s, "end": e, "heading_path": b.get("heading_path")})
        else:
            units.append({"start": b["start"], "end": b["end"], "heading_path": b.get("heading_path")})

    return _pack_units(units, text, max_tokens, overlap), {}


# --- 语义分割 -------------------------------------------------------------

#: 语义敏感度的默认值。75 在多数文档上给出"能看出语义转折、又不至于碎"的结果。
DEFAULT_SENSITIVITY = 75

#: markdown 哈希 -> (原子区间, 原子向量)。见 _atom_vectors 的说明。
_ATOM_CACHE: Dict[str, Tuple[List[Tuple[int, int]], List[List[float]]]] = {}

#: 供验证用的计数器（见 tasks.md 3.3）。
_EMBED_STATS = {"encode_calls": 0, "cache_hits": 0}

# 嵌入请求必须自己分批，不能把整篇文档一次发出去。
#
# 库的 REST 调用把读超时写死成 30 秒（embedding.py 里 requests.post(..., timeout=30)），
# 本机 Ollama 一次编码几十条中文长块必然超时。超时本身还算小事，麻烦的是
# index_chunks 的超时回退分支是坏的：它对 encode() 返回的 list[ndarray] 判断错误，
# 会把整批结果丢成零向量，而且每 8 条只补 1 条零向量。零向量对任何查询的余弦
# 相似度都是 0，永远检索不到；数量又对不上 metadata，add_vectors 按 zip 配对，
# 多出来的 chunk 被静默丢弃——最后仍打印 upsert 成功。
#
# 所以请求大小由这里控制，待在 30 秒以内，不去依赖那个回退分支。
#
# 8 是量出来的，不是拍的。本机实测（每块约 2700 字符，nomic-embed-text）：
#     batch= 8 -> 13.3s      batch=16 -> 27.0s（已贴近上限）
#     batch=32 -> 30.0s 读超时，复现了使用者的报错
# 吞吐约 1600 字符/秒且基本线性，所以减小批次几乎不损失总耗时，却换来成倍余量。
EMBED_BATCH_SIZE = 8      # 入库用：index_chunks 只暴露「条数」这一个口子
EMBED_CHAR_BUDGET = 6000  # 原子编码用：原子大小悬殊（整个代码块是一个原子），按字符数才管得住


def _batch_by_chars(texts: Sequence[str], budget: int) -> List[List[str]]:
    """按累计字符数分批。

    单个文本自己就超过 ``budget`` 时它独占一批——拆开它会改变语义，
    语义分割的切点位置就错了。
    """
    batches: List[List[str]] = []
    cur: List[str] = []
    cur_len = 0
    for t in texts:
        if cur and cur_len + len(t) > budget:
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(t)
        cur_len += len(t)
    if cur:
        batches.append(cur)
    return batches


def _protected_regions(text: str) -> List[Tuple[int, int]]:
    """找出必须整体保留的区间：围栏代码块与连续表格行。

    代码块和表格在语义上是一个整体。切开它们既产生无意义的向量，展示时
    也会把代码和表格割裂开——这正好是学习者在看切分结果时最不能接受的一种错。
    """
    regions: List[Tuple[int, int]] = []
    lines = text.splitlines(keepends=True)
    pos = 0
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        m = _FENCE_RE.match(line)
        if m:
            fence = m.group(1)
            start = pos
            pos += len(line)
            i += 1
            while i < n:
                closing = _FENCE_RE.match(lines[i])
                pos += len(lines[i])
                if closing and closing.group(1) == fence:
                    i += 1
                    break
                i += 1
            regions.append((start, pos))
            continue

        if line.lstrip().startswith("|"):
            start = pos
            while i < n and lines[i].lstrip().startswith("|"):
                pos += len(lines[i])
                i += 1
            regions.append((start, pos))
            continue

        pos += len(line)
        i += 1

    return regions


def _iter_atoms(text: str) -> List[Tuple[int, int]]:
    """切出语义分割的原子单元：句子级，但代码块与表格整体不拆。"""
    regions = _protected_regions(text)
    atoms: List[Tuple[int, int]] = []
    cursor = 0

    for r_start, r_end in regions:
        atoms.extend(_iter_sentences(text, cursor, r_start))
        atoms.append((r_start, r_end))
        cursor = r_end
    atoms.extend(_iter_sentences(text, cursor, len(text)))

    return [(s, e) for s, e in atoms if text[s:e].strip()]


def _normalize_vectors(vecs: Any) -> List[List[float]]:
    """把 embedder 的各种返回形状统一成 ``List[List[float]]``。

    实际见过三种返回：numpy 二维数组、numpy 一维数组的 list、以及单个
    文本时直接返回的一条向量。这里都要吃下，否则语义分割会以一个和模型
    无关的 TypeError 失败，很难排查。
    """
    if hasattr(vecs, "tolist"):
        vecs = vecs.tolist()
    if not isinstance(vecs, list):
        vecs = [vecs]
    # 一条扁平的向量（元素是数字）说明整体就是单个向量，不是向量的列表
    if vecs and isinstance(vecs[0], (int, float)):
        vecs = [vecs]

    out: List[List[float]] = []
    for v in vecs:
        if hasattr(v, "tolist"):
            v = v.tolist()
        if not isinstance(v, (list, tuple)):
            v = [v]
        out.append([float(x) for x in v])
    return out


def _atom_vectors(text: str) -> Tuple[List[Tuple[int, int]], List[List[float]]]:
    """原子单元及其向量，按 markdown 内容哈希缓存。

    这是全流程唯一昂贵且需要联网的一步。缓存之后，拖动「语义敏感度」滑块
    只重跑距离与阈值计算（纯 Python 数组运算，毫秒级）——没有这层缓存，
    滑块每动一次都要重新编码全文，这个功能实际上就没法用。
    """
    key = hashlib.md5(text.encode("utf-8")).hexdigest()
    if key in _ATOM_CACHE:
        _EMBED_STATS["cache_hits"] += 1
        return _ATOM_CACHE[key]

    atoms = _iter_atoms(text)
    if not atoms:
        return [], []

    # 用与入库相同的预处理，保证这里算出的距离反映的是真正入库的那些向量
    try:
        from hello_agents.memory.rag import pipeline as _p

        preprocess = getattr(_p, "_preprocess_markdown_for_embedding", None)
    except Exception:
        preprocess = None

    texts = []
    for s, e in atoms:
        piece = text[s:e]
        if callable(preprocess):
            try:
                piece = preprocess(piece)
            except Exception:
                pass
        texts.append(piece or text[s:e])

    try:
        embedder = get_text_embedder()
        vecs: List[List[float]] = []
        for part in _batch_by_chars(texts, EMBED_CHAR_BUDGET):
            _EMBED_STATS["encode_calls"] += 1
            vecs.extend(_normalize_vectors(embedder.encode(part)))
    except Exception as e:
        raise EmbeddingUnavailableError(
            f"语义分割需要向量模型，但调用失败：{e}。请检查 embedding 配置，"
            f"或改用「段落分割」「Markdown 结构感知」。"
        ) from e

    if len(vecs) != len(atoms):
        raise EmbeddingUnavailableError(
            f"向量数量({len(vecs)})与原子数量({len(atoms)})不一致，语义分割不可用。"
        )

    _ATOM_CACHE[key] = (atoms, vecs)
    return atoms, vecs


def embedding_stats() -> Dict[str, int]:
    """返回向量编码次数与缓存命中次数（验证用，见 tasks.md 3.3）。"""
    return dict(_EMBED_STATS)


def clear_embedding_cache() -> None:
    """清空原子向量缓存。"""
    _ATOM_CACHE.clear()


def _cosine_distances(vecs: Sequence[Sequence[float]]) -> List[float]:
    """相邻向量的余弦距离 ``1 - cos(v[i], v[i+1])``。"""
    out: List[float] = []
    for a, b in zip(vecs, vecs[1:]):
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        out.append(0.0 if na == 0 or nb == 0 else 1.0 - dot / (na * nb))
    return out


def _percentile(values: Sequence[float], pct: float) -> float:
    """线性插值分位数，语义与 ``numpy.percentile`` 一致（不引入 numpy 依赖）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (pct / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] * (hi - k) + ordered[hi] * (k - lo)


def _merge_small_groups(
    groups: List[List[Tuple[int, int]]], min_chars: int, max_chars: int
) -> List[List[Tuple[int, int]]]:
    """合并过小的块，但不让合并突破最大块上限。"""

    def span(g: List[Tuple[int, int]]) -> int:
        return sum(e - s for s, e in g)

    merged: List[List[Tuple[int, int]]] = []
    cur: List[Tuple[int, int]] = []
    cur_len = 0

    for g in groups:
        g_len = span(g)
        if not cur:
            cur, cur_len = list(g), g_len
            continue
        if cur_len < min_chars and cur_len + g_len <= max_chars:
            cur.extend(g)
            cur_len += g_len
        else:
            merged.append(cur)
            cur, cur_len = list(g), g_len
    if cur:
        merged.append(cur)

    # 收尾：最后一块偏小时并进前一块，前提是并完不超上限
    if len(merged) >= 2:
        last_len = span(merged[-1])
        if last_len < min_chars and span(merged[-2]) + last_len <= max_chars:
            merged[-2].extend(merged[-1])
            merged.pop()

    return merged


def split_semantic(
    text: str, params: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """语义分割：按相邻文本单元的向量距离找语义断点。

    流程：原子单元 -> 向量化（带缓存）-> 相邻余弦距离 -> 分位数阈值 -> 尺寸兜底。

    阈值取分位数而不是绝对值：绝对阈值对文档极其敏感，换一份文档就可能
    一处不切或切得粉碎。用分位数之后，敏感度在任何文档上都表示同一个相对
    含义（「切在距离排名前 (100-敏感度)% 的断点上」），参数才能跨文档迁移
    ——这也是滑块可以有意义地标成百分比的原因。

    方向：敏感度越高 -> 阈值分位越低 -> 更多断点被判为切点。
    """
    # 阈值分位 = 100 - 敏感度。敏感度 75 表示切在距离高于 25% 分位处。
    # 写成减法而不是直接用敏感度，是因为「敏感度」的直觉是「越高越容易
    # 判定转折」，而分位数越大阈值越高、切点越少，两者方向相反。
    sensitivity = float(params.get("sensitivity", DEFAULT_SENSITIVITY))
    min_chars = int(params.get("min_chars", 200))
    max_chars = int(params.get("max_chars", 1600))

    sensitivity = min(99.0, max(1.0, sensitivity))
    if min_chars < 1:
        min_chars = 1
    if max_chars < min_chars:
        max_chars = min_chars

    atoms, vecs = _atom_vectors(text)
    if not atoms:
        return [], {}

    diagnostics: Dict[str, Any] = {"atom_count": len(atoms)}

    if len(atoms) == 1:
        groups = [[atoms[0]]]
        diagnostics.update({"distances": [], "threshold": 0.0, "cut_indices": []})
    else:
        distances = _cosine_distances(vecs)
        tau = _percentile(distances, 100.0 - sensitivity)
        cuts = [i for i, d in enumerate(distances) if d > tau]

        groups = []
        prev = 0
        for c in cuts:
            groups.append(atoms[prev:c + 1])
            prev = c + 1
        groups.append(atoms[prev:])

        diagnostics.update({
            "distances": distances,
            "threshold": tau,
            "cut_indices": cuts,
        })

    groups = _merge_small_groups(groups, min_chars, max_chars)

    # 尺寸兜底：把仍超上限的块按句末标点继续拆（这里按字符计，因为
    # 语义策略的最小/最大块参数就是以字符计的）
    final_units: List[List[Tuple[int, int]]] = []
    for g in groups:
        g_start, g_end = g[0][0], g[-1][1]
        if (g_end - g_start) > max_chars:
            pieces = _split_oversized(text, g_start, g_end, max_chars, len)
            final_units.append(pieces)
        else:
            final_units.append(g)

    headings = _heading_index(text)
    chunks: List[Dict[str, Any]] = []
    for g in final_units:
        start, end = g[0][0], g[-1][1]
        chunks.append({
            "content": text[start:end],
            "start": start,
            "end": end,
            "heading_path": _heading_at(headings, start),
        })

    # 尺寸兜底会合并掉一部分候选切点。曲线要标的是真正成为 chunk 边界的那几处
    # ——标候选点会得到「图上十几个红点、结果只有一块」这种看不懂的图。
    # 被合并掉的数量单独记下来，由渲染层说明原因（尺寸约束），
    # 否则使用者会以为敏感度滑块失灵了。
    if diagnostics.get("distances"):
        distances = diagnostics["distances"]
        end_to_atom = {e: i for i, (s, e) in enumerate(atoms)}
        surviving: List[int] = []
        for ch in chunks[:-1]:
            idx = end_to_atom.get(ch["end"])
            if idx is not None and idx < len(distances):
                surviving.append(idx)
        candidates = diagnostics["cut_indices"]
        diagnostics["candidate_cut_indices"] = candidates
        diagnostics["merged_cut_count"] = len([c for c in candidates if c not in set(surviving)])
        diagnostics["cut_indices"] = sorted(set(surviving))

    return chunks, diagnostics


# ===========================================================================
# 7. 策略注册表
# ===========================================================================

#: 策略名 -> {label, split, params}。
#: ``params`` 描述该策略的参数控件，UI 直接照着生成面板，所以「切换策略时
#: 参数面板跟着变」是注册表的自然结果，不需要为每个策略单独写一遍界面。
STRATEGIES: Dict[str, Dict[str, Any]] = {
    "paragraph": {
        "label": "段落分割",
        "split": split_paragraph,
        "description": "按空行分隔的自然段落聚合，最简单直观的切法。",
        "params": [
            {
                "name": "chunk_size", "label": "块大小", "kind": "slider",
                "default": 800, "min": 100, "max": 2000, "step": 50,
                "unit": "token", "info": "单个 chunk 的目标上限，按 token 计。",
            },
            {
                "name": "chunk_overlap", "label": "重叠大小", "kind": "slider",
                "default": 80, "min": 0, "max": 400, "step": 10,
                "unit": "token", "info": "相邻 chunk 之间保留的重叠量。",
            },
        ],
    },
    "markdown": {
        "label": "Markdown 结构感知",
        "split": split_markdown,
        "description": "按标题层级与段落切分，保留章节归属。一键加载所用的默认策略。",
        "params": [
            {
                "name": "chunk_size", "label": "块大小", "kind": "slider",
                "default": 800, "min": 100, "max": 2000, "step": 50,
                "unit": "token", "info": "单个 chunk 的目标上限，按 token 计。",
            },
            {
                "name": "chunk_overlap", "label": "重叠大小", "kind": "slider",
                "default": 80, "min": 0, "max": 400, "step": 10,
                "unit": "token", "info": "相邻 chunk 之间保留的重叠量。",
            },
        ],
    },
    "semantic": {
        "label": "语义分割",
        "split": split_semantic,
        "description": "按相邻文本的向量语义距离找断点，真实调用 embedding 模型。",
        "params": [
            {
                "name": "sensitivity", "label": "语义敏感度", "kind": "slider",
                "default": DEFAULT_SENSITIVITY, "min": 50, "max": 99, "step": 1,
                "unit": "%",
                "info": "越高越容易把语义转折判为切点，chunk 越多；越低则只切最明显的转折。",
            },
            {
                "name": "min_chars", "label": "最小块", "kind": "number",
                "default": 200, "min": 1, "max": 5000, "step": 50,
                "unit": "字符", "info": "小于这个长度的块会与邻居合并。",
            },
            {
                "name": "max_chars", "label": "最大块", "kind": "number",
                "default": 1600, "min": 100, "max": 20000, "step": 100,
                "unit": "字符", "info": "超过这个长度的块会被继续拆分。",
            },
        ],
    },
}

#: 默认策略：与库原有行为最接近的一种，也是快速路径所用的策略。
DEFAULT_STRATEGY = "markdown"


def default_params(strategy: str) -> Dict[str, Any]:
    """返回某策略的默认参数字典。"""
    spec = STRATEGIES.get(strategy) or STRATEGIES[DEFAULT_STRATEGY]
    return {p["name"]: p["default"] for p in spec["params"]}


def param_specs(strategy: str) -> List[Dict[str, Any]]:
    """返回某策略的参数控件描述，供 UI 直接生成面板。"""
    spec = STRATEGIES.get(strategy) or STRATEGIES[DEFAULT_STRATEGY]
    return list(spec["params"])


def split_document(
    markdown: str, strategy: str, params: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """按指定策略切分 markdown。

    Returns:
        ``(chunks, diagnostics)``。``diagnostics`` 只有语义策略会填内容，
        渲染层据此决定要不要画距离曲线——所以界面不需要按策略写分支。
    """
    if strategy not in STRATEGIES:
        raise ChunkingError(f"未知的切分策略: {strategy}")
    if not markdown or not markdown.strip():
        raise EmptyContentError("文档内容为空，无法切分")

    merged = default_params(strategy)
    merged.update(params or {})
    chunks, diagnostics = STRATEGIES[strategy]["split"](markdown, merged)

    if not chunks:
        raise EmptyContentError("切分后没有得到任何 chunk")
    return chunks, diagnostics


# ===========================================================================
# 8. 物化：切分结果 -> 可入库的 dict
# ===========================================================================

def materialize(
    chunks: List[Dict[str, Any]],
    file_path: str,
    markdown_text: str,
    namespace: str = "default",
    strategy: str = "",
    params: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """把切分器的产出变成 ``index_chunks`` 能吃的 dict。

    这是**唯一的物化点**。预览渲染和入库读的是同一个 list，所以预览看到的
    就是将要入库的东西，不存在第二条计算路径。

    元数据的键刻意与库的 ``load_and_chunk_texts`` 保持一致，其中包括那四个
    决定检索可见性的标签所依赖的字段。那四个标签本身（``memory_type`` /
    ``is_rag_data`` / ``data_source`` / ``rag_namespace``）由 ``index_chunks``
    在写入时注入——这里不重复设置，也就不会漏。

    Raises:
        EmptyContentError: 全部 chunk 都是空白。
    """
    ext = (os.path.splitext(file_path)[1] or "").lower()
    lang = _detect_lang(markdown_text)
    doc_id = hashlib.md5(f"{file_path}|{len(markdown_text)}".encode("utf-8")).hexdigest()

    out: List[Dict[str, Any]] = []
    seen: set = set()

    for ch in chunks:
        content = ch["content"]
        norm = content.strip()
        if not norm:
            continue

        content_hash = hashlib.md5(norm.encode("utf-8")).hexdigest()
        if content_hash in seen:
            continue
        seen.add(content_hash)

        start = ch.get("start", 0)
        end = ch.get("end", start + len(content))
        chunk_id = hashlib.md5(
            f"{doc_id}|{start}|{end}|{content_hash}".encode("utf-8")
        ).hexdigest()

        out.append({
            "id": chunk_id,
            "content": content,
            "metadata": {
                "source_path": file_path,
                "file_ext": ext,
                "doc_id": doc_id,
                "lang": lang,
                "start": start,
                "end": end,
                "content_hash": content_hash,
                "namespace": namespace or "default",
                "source": "rag",
                "external": True,
                "heading_path": ch.get("heading_path"),
                "format": "markdown",
                # 本次新增：记录产出这块的切分方式，入库后仍可回溯
                "split_strategy": strategy,
                "split_params": dict(params or {}),
            },
        })

    if not out:
        raise EmptyContentError("切分后没有得到任何有效 chunk")
    return out


def _detect_lang(sample: str) -> str:
    """语言探测，转发库的实现；库不可用时返回 unknown。"""
    try:
        from hello_agents.memory.rag import pipeline as _p

        fn = getattr(_p, "_detect_lang", None)
        if callable(fn):
            return fn(sample)
    except Exception:
        pass
    return "unknown"


# ===========================================================================
# 9. 预览指纹
# ===========================================================================

def preview_fingerprint(
    file_path: str, markdown: str, strategy: str, params: Dict[str, Any]
) -> Dict[str, Any]:
    """预览指纹：任何影响切分结果的输入变了，指纹就变。

    入库前比对指纹，可以挡住「改了参数但没重新预览就直接入库」——
    否则入库的和看到的就不是一回事了，而整个方案二的前提正是这两者一致。
    指纹里含 markdown 哈希，所以重传同名但内容不同的文件也会让预览失效。
    """
    return {
        "file_path": os.path.abspath(file_path),
        "markdown_hash": hashlib.md5(markdown.encode("utf-8")).hexdigest(),
        "strategy": strategy,
        "params": {k: v for k, v in sorted((params or {}).items())},
    }


# ===========================================================================
# 10. 入库：清旧、写新
# ===========================================================================

def _document_point_ids(store, file_path: str, namespace: str) -> List[str]:
    """按 ``source_path`` + ``rag_namespace`` 分页取出某文档的全部 point id。

    集合是全部命名空间共享的（单集合 + ``rag_namespace`` 载荷标签），所以必须同时
    按命名空间过滤：只按 ``source_path`` 的话，同一个路径被两个用户各自入库时，
    这里会取到对方的 point——而本函数是删除路径的前置枚举，会删掉对方的数据。
    """
    from qdrant_client.http.models import FieldCondition, Filter, MatchValue

    scroll_filter = Filter(
        must=[
            FieldCondition(key="source_path", match=MatchValue(value=file_path)),
            FieldCondition(key="rag_namespace", match=MatchValue(value=namespace)),
        ]
    )

    ids: List[str] = []
    offset = None
    while True:
        points, next_offset = store.client.scroll(
            collection_name=store.collection_name,
            scroll_filter=scroll_filter,
            limit=100,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        if points:
            ids.extend(str(p.id) for p in points)
        if next_offset is None:
            break
        offset = next_offset
    return ids


def count_document_chunks(store, file_path: str, namespace: str) -> int:
    """回读某文档在指定命名空间里实际有多少条 chunk。"""
    return len(_document_point_ids(store, file_path, namespace))


def list_documents(store, namespace: str) -> List[Dict[str, Any]]:
    """列出某命名空间下已入库的文档，返回 ``[{source_path, chunk_count}, ...]``。

    集合是全部命名空间共享的（单集合 + ``rag_namespace`` 载荷标签），所以先按
    ``rag_namespace`` 过滤、分页扫描，再按 ``source_path`` 聚合。既有的
    ``_document_point_ids`` 必须传入 ``file_path``，全项目没有「列出本命名空间有
    哪些文档」这条路径（design.md D13）——而这正是「逐个查看每一份文档的质量」
    所缺的入口，也是重启后唯一还在的清单来源。
    """
    from qdrant_client.http.models import FieldCondition, Filter, MatchValue

    scroll_filter = Filter(
        must=[FieldCondition(key="rag_namespace", match=MatchValue(value=namespace))]
    )

    counts: Dict[str, int] = {}
    offset = None
    while True:
        points, next_offset = store.client.scroll(
            collection_name=store.collection_name,
            scroll_filter=scroll_filter,
            limit=256,
            offset=offset,
            with_payload=["source_path"],
            with_vectors=False,
        )
        for p in points:
            source_path = (p.payload or {}).get("source_path")
            if source_path:
                counts[source_path] = counts.get(source_path, 0) + 1
        if next_offset is None:
            break
        offset = next_offset

    return [
        {"source_path": source_path, "chunk_count": n}
        for source_path, n in sorted(counts.items())
    ]


def stored_text_len(
    source_path: Optional[str], chunks: Sequence[Dict[str, Any]]
) -> Optional[int]:
    """已存 chunk 的偏移所对应的文本长度；无法确定时返回 ``None``。

    最可靠的检查是拿原文比对：``markdown[start:end] == content``。**但「原文」是
    哪一份必须说清楚**——偏移只在一份具体的文本里有意义，而这份文档的 chunk 可能
    来自两份文本中的任意一份：

    - 本变更之后入库的 → 切自**清洗后**的文本
    - 本变更之前入库的存量内容 → 切自**清洗前**的原始提取

    两份都要试。只试清洗后的那份，会把**所有存量内容**误判成位置不可用——实测
    清洗一上线，封思敏 87/87、唐斯琪 91/91、朱光熙 21/21 的偏移立刻从成立翻成
    不成立，因为那些 chunk 本来就是对着原始提取切的。

    返回的是**匹配上的那一份的长度**，覆盖率的分母必须跟着它走：拿清洗后的长度
    去除以清洗前的偏移，会算出 >100% 的覆盖率（封思敏 172,500 ÷ 163,545 = 105.5%）。

    原文取不到时（来源文件已随 Gradio 临时目录清理而消失）返回 ``None``——宁可
    不出覆盖率，也不拿一个不知道对不对的分母算。

    存量 ``default`` 命名空间的两份文档实测全部错位（天地融合 0/19、冀博 0/2），
    形态是 ``content`` 对应 ``markdown[start+1:end+1]``——它们是早期脚本入的库。
    """
    if not chunks:
        return None
    if any(
        not isinstance(c.get("start"), int) or not isinstance(c.get("end"), int)
        for c in chunks
    ):
        return None
    if not source_path or not os.path.exists(source_path):
        return None

    try:
        record = _extract_record(source_path)
    except ChunkingError:
        return None

    for candidate in (record.text, record.raw_text):
        if all(
            candidate[c["start"]:c["end"]] == (c.get("content") or "")
            for c in chunks
        ):
            return len(candidate)
    return None


def assess_stored_document(
    source_path: Optional[str], chunks: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    """从**已存 chunk 的文本**度量一份文档的提取质量（design.md D12）。

    回看路径的 chunk 来自 Qdrant，从不经过 ``extract_markdown``，所以质量报告不能
    从提取缓存取——那份缓存活在内存里，上次会话入库、本次会话未再提取的文档在缓存
    里是空的，而它恰恰是最需要告警的情形。这里直接量已存内容，回答一个更贴切的
    问题：「知识库里这份文档现在是脏的么」。重新入库后量出来就是干净的，自洽。

    含噪声时不代表「提取器还在出错」，而是「这份内容入库时**还没清洗**」——文案上
    必须说清是后者，否则沉默会被读成「库里是干净的」。

    ``text_len`` 是偏移匹配上的那份文本的长度，供覆盖率做分母；位置不可信时为
    ``None``，调用方据此不出覆盖率。
    """
    text = "".join(c.get("content") or "" for c in chunks)
    text_len = stored_text_len(source_path, chunks)
    return {
        "quality": assess_extraction_quality(text),
        "position_trustworthy": text_len is not None,
        "text_len": text_len,
        "chunk_count": len(chunks),
        "char_count": len(text),
    }


def purge_document(store, file_path: str, namespace: str) -> List[str]:
    """清除某文档在指定命名空间中的全部 chunk，返回被删除的 id 列表。"""
    ids = _document_point_ids(store, file_path, namespace)
    if ids:
        if not store.delete_vectors(ids):
            raise IndexingError(f"清理旧 chunk 失败（{len(ids)} 条），未改动知识库")
    return ids


def get_store(rag_tool):
    """取出 RAG 工具底层的向量存储。

    和 11_Q&A_Assistant.py 里既有的做法一致：经由 ``_get_pipeline`` 拿 store。
    这个私有方法在本项目里已被依赖，且两个 hello_agents 副本都提供它。
    """
    pipeline = rag_tool._get_pipeline(rag_tool.rag_namespace)
    return pipeline["store"]


class ChunkingWorkbench:
    """切分工作台的会话状态：预览结果 + 指纹。

    「预览了什么」和「将要入库什么」都记在这里，UI 只负责调用。把状态放在
    一个对象里而不是散在 Gradio 闭包中，是为了让「预览与入库一致」这条
    约束有一个明确的落点，也方便单独测试。
    """

    def __init__(self, namespace: str = "default"):
        self.namespace = namespace
        self.reset()

    def reset(self) -> None:
        """清空会话状态。入库之后也会调，避免旧预览被重复入库。"""
        self.file_path: Optional[str] = None
        self.markdown: str = ""
        self.strategy: Optional[str] = None
        self.params: Dict[str, Any] = {}
        self.chunks: List[Dict[str, Any]] = []
        self.materialized: List[Dict[str, Any]] = []
        self.diagnostics: Dict[str, Any] = {}
        self.fingerprint: Optional[Dict[str, Any]] = None

    def preview(
        self, file_path: str, strategy: str, params: Dict[str, Any]
    ) -> "ChunkingWorkbench":
        """提取 -> 切分 -> 物化，全程不碰数据库。

        预览与入库共用这里产出的 ``materialized``，所以看到什么就入库什么。
        """
        markdown = extract_markdown(file_path)
        chunks, diagnostics = split_document(markdown, strategy, params)
        materialized = materialize(
            chunks, file_path, markdown, self.namespace, strategy, params
        )

        self.file_path = file_path
        self.markdown = markdown
        self.strategy = strategy
        self.params = dict(params or {})
        self.chunks = chunks
        self.materialized = materialized
        self.diagnostics = diagnostics
        self.fingerprint = preview_fingerprint(file_path, markdown, strategy, params)
        return self

    def is_fresh(
        self, file_path: Optional[str], strategy: str, params: Dict[str, Any]
    ) -> bool:
        """当前选择是否仍与上次预览一致。

        重新提取一次 markdown 来比对：提取有缓存（按 mtime 与大小），所以
        这一步很便宜，同时又能发现「文件被换掉了」这种情况——重传同名但
        内容不同的文件会让指纹失效。
        """
        if self.fingerprint is None or not self.materialized:
            return False
        target = file_path or self.file_path
        if not target:
            return False
        try:
            markdown = extract_markdown(target)
        except ChunkingError:
            return False
        return self.fingerprint == preview_fingerprint(target, markdown, strategy, params)

    def commit(self, store) -> Tuple[int, int]:
        """把预览过的那份 chunk 写进知识库。

        调用方必须先用 ``is_fresh`` 确认预览没过期。

        Returns:
            ``(写入的 chunk 数, 清理掉的旧 chunk 数)``
        """
        if not self.materialized or not self.file_path:
            raise ChunkingError("尚未预览，无法入库")

        file_path = self.file_path
        materialized = self.materialized
        count = len(materialized)

        removed = index_document(store, materialized, file_path, self.namespace)
        self.reset()
        return count, removed


def index_document(
    store,
    materialized: List[Dict[str, Any]],
    file_path: str,
    namespace: str,
) -> int:
    """把切分结果写入知识库：先清理该文档的旧 chunk，再写新的。

    顺序上切分早已在调用方完成，所以切分失败时数据库毫发无伤。

    清理与写入之间不是原子的：中途失败会让该文档从知识库消失。这里把这种
    情形明确抛成 ``IndexingError``，由调用方如实告诉使用者「需要重新入库」，
    而不是静默当成功。为本地教学项目引入事务回滚不划算，如实报错更诚实。

    Returns:
        被清理掉的旧 chunk 数量。
    """
    from hello_agents.memory.rag.pipeline import index_chunks

    removed = purge_document(store, file_path, namespace)
    try:
        index_chunks(
            store=store,
            chunks=materialized,
            rag_namespace=namespace,
            batch_size=EMBED_BATCH_SIZE,
        )
    except Exception as e:
        raise IndexingError(
            f"旧切分已清除，但新内容写入失败：{e}。该文档当前处于未完成入库状态，"
            f"请重新入库。"
        ) from e

    # index_chunks 不会因为向量降级而抛异常：库在编码超时后会塞零向量然后照常
    # 返回，连 upsert 成功都照打印。所以这里回读一遍实际写入的条数——少了就说明
    # 发生了降级，此时如实报错，而不是让一份残文档冒充入库成功。
    written = count_document_chunks(store, file_path, namespace)
    if written != len(materialized):
        raise IndexingError(
            f"入库不完整：应写入 {len(materialized)} 条，实际只有 {written} 条，"
            f"其余向量在编码超时后退化丢失。该文档当前处于未完成入库状态，"
            f"请确认 embedding 服务可用后重新入库。"
        )
    return len(removed)
