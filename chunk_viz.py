#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""切分结果的可视化渲染。

这里全是纯函数：输入 chunk 列表，输出 HTML 字符串，不依赖任何实例状态。
切分工作台的本地预览和「🏠 开始使用」里从 Qdrant 回看，用的是同一组函数，
所以两条路径看到的东西必然一致，能力也不会一边有一边没有。

语义分割额外画一条「相邻语义距离」曲线：卡片能说明切成了什么样，但说明不了
为什么切在这儿。切点是算出来的，把依据画出来，使用者才能对「敏感度」这个
参数建立直觉——拖动滑块时虚线上下移动、切点增减，参数的作用立刻可见。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence


def _escape_html(text: str) -> str:
    """转义 HTML 特殊字符。"""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_CSS = """
<style>
    .cv-container {
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        padding: 16px; max-width: 100%; box-sizing: border-box;
    }
    .cv-header {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white; padding: 20px 24px; border-radius: 12px;
        margin-bottom: 20px; box-shadow: 0 4px 15px rgba(102, 126, 234, 0.3);
    }
    .cv-header h2 { margin: 0 0 12px 0; font-size: 20px; font-weight: 600; }
    .cv-header .doc-name { font-size: 14px; opacity: 0.9; margin-bottom: 8px; }
    .cv-stats { display: flex; gap: 16px; flex-wrap: wrap; margin-top: 12px; }
    .cv-stat {
        background: rgba(255,255,255,0.2); border-radius: 8px;
        padding: 8px 16px; text-align: center;
    }
    .cv-stat .v { font-size: 24px; font-weight: 700; display: block; }
    .cv-stat .l { font-size: 12px; opacity: 0.85; display: block; }
    .cv-params {
        background: #f0f4ff; border: 1px solid #d0d9f0; border-radius: 8px;
        padding: 12px 16px; margin-bottom: 20px; font-size: 13px;
        color: #4a5568; display: flex; gap: 24px; flex-wrap: wrap;
    }
    .cv-params span { display: inline-flex; align-items: center; gap: 4px; }
    .cv-section {
        border: 1px solid #e2e8f0; border-radius: 10px; padding: 14px 16px;
        margin-bottom: 20px; background: #fbfcfe;
    }
    .cv-section h3 {
        margin: 0 0 4px 0; font-size: 14px; color: #4a5568; font-weight: 600;
    }
    .cv-section .hint { font-size: 12px; color: #a0aec0; margin-bottom: 8px; }
    .cv-card {
        background: white; border: 1px solid #e2e8f0; border-radius: 10px;
        margin-bottom: 12px; overflow: hidden; transition: box-shadow 0.2s;
    }
    .cv-card:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.1); }
    .cv-card-head {
        background: #f7fafc; padding: 10px 16px; display: flex;
        justify-content: space-between; align-items: center;
        flex-wrap: wrap; gap: 8px; border-bottom: 1px solid #e2e8f0;
    }
    .cv-card-no {
        font-weight: 700; font-size: 14px; color: #4a5568;
        display: flex; align-items: center; gap: 8px;
    }
    .cv-idx {
        background: #667eea; color: white; border-radius: 50%;
        width: 28px; height: 28px; display: inline-flex;
        align-items: center; justify-content: center;
        font-size: 12px; font-weight: 700;
    }
    .cv-badges { display: flex; gap: 8px; flex-wrap: wrap; }
    .cv-badge {
        font-size: 11px; padding: 3px 10px; border-radius: 12px; font-weight: 500;
    }
    .cv-badge.chars { background: #ebf4ff; color: #3182ce; }
    .cv-badge.pos { background: #f0fff4; color: #276749; }
    .cv-badge.overlap { background: #fffbeb; color: #975a16; }
    .cv-heading {
        font-size: 11px; color: #a0aec0; margin-top: 4px; font-style: italic;
    }
    .cv-body {
        padding: 14px 16px; font-size: 13px; line-height: 1.7; color: #2d3748;
        white-space: pre-wrap; word-break: break-word; max-height: 200px;
        overflow-y: auto; background: #fefefe;
        border-left: 3px solid #667eea; margin: 0;
        font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    }
    .cv-overlap-bar {
        background: linear-gradient(90deg, #fef5e7 0%, #fdf2f8 100%);
        padding: 6px 16px; font-size: 11px; color: #975a16;
        border-top: 1px dashed #f6ad55;
    }
    .cv-empty { color: #975a16; padding: 20px; }
    .cv-quality {
        background: #fffaf0; border: 1px solid #f6ad55; border-left: 4px solid #dd6b20;
        border-radius: 8px; padding: 12px 16px; margin-bottom: 20px;
        font-size: 13px; color: #744210; line-height: 1.6;
    }
    .cv-quality-head { font-weight: 700; font-size: 14px; color: #c05621; margin-bottom: 6px; }
    .cv-quality-reasons { margin: 0 0 8px 0; padding-left: 20px; }
    .cv-quality p { margin: 4px 0 0 0; }
    .cv-vision {
        background: #f7fafc; border: 1px solid #cbd5e0; border-left: 4px solid #4a5568;
        border-radius: 8px; padding: 12px 16px; margin-bottom: 20px;
        font-size: 13px; color: #2d3748; line-height: 1.6;
    }
    .cv-vision-head { font-weight: 700; font-size: 14px; color: #2d3748; margin-bottom: 6px; }
    .cv-vision p { margin: 4px 0 0 0; }
    .cv-vision table { border-collapse: collapse; width: 100%; margin-top: 8px; font-size: 12px; }
    .cv-vision th, .cv-vision td {
        border-bottom: 1px solid #e2e8f0; padding: 3px 8px; text-align: right;
    }
    .cv-vision th { color: #4a5568; font-weight: 600; }
    .cv-vision th:first-child, .cv-vision td:first-child { text-align: left; }
    .cv-vision tr.is-empty td { color: #975a16; }
    .cv-vision tr.is-failed td { color: #c53030; font-weight: 600; }
    .cv-vision details { margin-top: 8px; }
    .cv-vision summary { cursor: pointer; color: #4a5568; }
    .cv-vision-note { color: #718096; font-size: 12px; }
</style>
"""


def render_param_bar(strategy_label: str, params: Dict[str, Any],
                     specs: Sequence[Dict[str, Any]]) -> str:
    """渲染参数条。

    每一项的单位取自参数规格而不是写死，这样界面上不会出现「chunk_size: 800
    字符」这种把 token 说成字符的错标——库的 _approx_token_len 表明这个参数
    一直是按 token 计的。
    """
    parts = [f"<span>🔧 <strong>切分参数</strong></span>"]
    for spec in specs:
        name = spec["name"]
        if name not in params:
            continue
        unit = spec.get("unit", "")
        value = params[name]
        parts.append(f"<span>{_escape_html(spec['label'])}: <strong>{_escape_html(value)}</strong> { _escape_html(unit)}</span>")
    parts.append(f"<span>📝 分割方式: <strong>{_escape_html(strategy_label)}</strong></span>")
    return f'<div class="cv-params">{"".join(parts)}</div>'


def render_stats(
    chunks: Sequence[Dict[str, Any]], coverage: Optional[Dict[str, Any]] = None
) -> str:
    """渲染 top 统计：chunk 总数、总字符数、平均每 chunk 字符数、内容覆盖率。

    ``coverage`` 是 ``chunking.assess_coverage()`` 的返回值，给了才多出「内容覆盖率」
    一格。取并集口径（被 chunk 位置区间覆盖的字符数 ÷ 文本长度），因此恒 ≤ 100%
    ——重叠不计入。**这一格几乎不动**（实测六份 99.67%–100.00%），它回答的是
    「切分有没有整段漏掉内容」，不是「切分好不好」；``chunk_overlap`` 的代价要看
    冗余，而冗余放在悬停提示里，不另占一格。

    保留两位小数：一位小数会把 99.67% 与 100.00% 一起显示成「100.0%」，等于白放。

    位置不可信或根本拿不到（如回看路径的来源文件已被清理）时 ``coverage`` 为
    ``None``，该格不出现：宁可不显示，也不显示一个编出来的分母。
    """
    total = len(chunks)
    total_chars = sum(len(c.get("content", "")) for c in chunks)
    avg = total_chars // total if total else 0

    keep = ""
    if coverage and coverage.get("coverage_ratio") is not None:
        covered = coverage["covered_chars"]
        text_len = coverage["text_len"]
        redundancy = coverage.get("redundancy_ratio")
        tip = (
            f"被 chunk 覆盖 {covered:,} 字 ÷ 文本 {text_len:,} 字；"
            f"未覆盖 {text_len - covered:,} 字（重叠不计入）"
        )
        if redundancy is not None:
            tip += f"；重叠冗余 +{redundancy * 100:.1f}%（chunk_overlap 所致）"
        keep = (
            f'<div class="cv-stat"><span class="v">'
            f'{coverage["coverage_ratio"] * 100:.2f}%</span>'
            f'<span class="l" title="{_escape_html(tip)}">内容覆盖率</span></div>'
        )
    return f"""
        <div class="cv-stats">
            <div class="cv-stat"><span class="v">{total}</span><span class="l">总 Chunks 数</span></div>
            <div class="cv-stat"><span class="v">{total_chars:,}</span><span class="l">总字符数</span></div>
            <div class="cv-stat"><span class="v">{avg}</span><span class="l">平均字符/Chunk</span></div>
            {keep}
        </div>
    """


def render_quality_block(
    report: Optional[Dict[str, Any]], stored: bool = False
) -> str:
    """渲染提取质量告警区块；判定合格或没有报告时返回空串。

    「合格时不打扰」是一条要求而不是可选打磨：健康文档的预览必须与改动前完全一致。

    ``stored=True`` 用于回看路径——那份额度是从**已存 chunk 的文本**量出来的，不是
    本次提取的结果。文案必须说清「库中这份内容尚未清洗、重新入库才生效」，否则沉默
    会被读成「库里是干净的」（design.md D12）。
    """
    if not report or not report.get("flagged"):
        return ""

    before = report.get("before") or {}
    after = report.get("after") or {}
    counts = report.get("counts") or {}
    reasons = "".join(
        f"<li>{_escape_html(r)}</li>" for r in report.get("reasons", [])
    )

    if stored:
        title = "⚠️ 提取质量告警：知识库里这份内容含编码噪声"
        lead = (
            "这份内容是<strong>本变更之前入库</strong>的，"
            "库中<strong>尚未清洗</strong>——重新入库后清洗才会生效。"
        )
    else:
        title = "⚠️ 提取质量告警：这份文本含编码噪声"
        lead = (
            f"本次已清洗：移除私用区字符 "
            f"<strong>{counts.get('pua_removed', 0):,}</strong> 个、"
            f"全角拉丁与数字转半角 "
            f"<strong>{counts.get('fullwidth_folded', 0):,}</strong> 个、"
            f"折叠汉字间空格 "
            f"<strong>{counts.get('han_space_folded', 0):,}</strong> 处。"
            f"入库的是清洗后的文本。"
        )

    return f"""
        <div class="cv-quality">
            <div class="cv-quality-head">{title}</div>
            <ul class="cv-quality-reasons">{reasons}</ul>
            <p>{lead}</p>
            <p>清洗<strong>不修复表格结构</strong>，它只去编码噪声：清洗后空壳表格率
               {after.get('hollow_table_ratio', 0):.1%}、表内字符占比
               {after.get('table_char_ratio', 0):.1%}。噪声清掉之后，原先靠占位符
               撑着的空壳格反而更显形，所以这两个数可能比清洗前更差——那不是清洗
               弄坏的，是本来就坏、只是之前看不出。这类损伤本次不修，这份文档的
               检索质量可能仍不理想。</p>
        </div>
    """


def summarize_quality(
    report: Optional[Dict[str, Any]], stored: bool = False
) -> str:
    """质量告警的一行文本摘要，供预览与一键加载的状态串取用；合格时返回空串。

    与 :func:`render_quality_block` 是同一个判定的两种媒介：HTML 区块给面板，
    这一行给状态文本。措辞保持一致，免得同一个事实在两处说法不同。
    """
    if not report or not report.get("flagged"):
        return ""

    reasons = "；".join(report.get("reasons") or [])
    if stored:
        tail = "库中这份内容尚未清洗，重新入库后才会生效。"
    else:
        counts = report.get("counts") or {}
        tail = (
            f"本次已清洗：移除私用区 {counts.get('pua_removed', 0):,} 个、"
            f"全角转半角 {counts.get('fullwidth_folded', 0):,} 个、"
            f"折叠汉字间空格 {counts.get('han_space_folded', 0):,} 处；"
            "表格结构未修复。"
        )
    return f"⚠️ 提取质量告警：{reasons}。{tail}"


def quality_label(quality: Optional[Dict[str, Any]]) -> str:
    """下拉框选项标签用的紧凑告警标记；合格时返回空串。

    下拉框渲染不了表格，选项标签是唯一能承载度量的位置（tasks.md 3.6），所以
    这里只留两个占比——阈值与处置在选中之后的报告里。

    ``quality`` 可以有两种形状，两者的两个占比与 ``flagged`` 同名同义：
    ``_quality_report`` 的整份报告（预览路径，占比在其 ``before`` 段里），
    或 ``assess_extraction_quality`` 的直接返回值（回看与统计路径）。认两种是
    为了不逼调用方为了一个标签去拼一份报告——但两种形状的取值必须一致，
    否则标签会静默显示 0.0%（实测踩过）。
    """
    if not quality:
        return ""
    metrics = quality.get("before") or quality
    if not metrics.get("flagged"):
        return ""
    return (
        f"⚠️ PUA {metrics.get('pua_ratio', 0.0):.1%} "
        f"全角 {metrics.get('fullwidth_ratio', 0.0):.1%}"
    )


def summarize_coverage(coverage: Optional[Dict[str, Any]]) -> str:
    """覆盖率低于阈值时的一行提醒；拿不到或覆盖完整时返回空串。

    「覆盖完整」按 ``assess_coverage`` 的 ``below_threshold`` 判定，所以健康文档
    的状态串与改动前逐字相同——合格时不打扰是一条要求，不是打磨。
    """
    if not coverage or coverage.get("coverage_ratio") is None:
        return ""
    if not coverage.get("below_threshold"):
        return ""
    return (
        f"⚠️ 内容覆盖率 {coverage['coverage_ratio']:.2%}"
        f"（未覆盖 {coverage['text_len'] - coverage['covered_chars']:,} 字）"
    )


#: 逐页结局的显示名。键就是 ``vision.PageResult.status`` 的三个取值——这里是
#: 渲染层，不 import 那个模块（它还带 pypdfium2 与 PIL），所以取了这样一份
#: 映射而不是直接引用常量；取值变了这里会显示原始字面量，不会静默显示成别的东西。
#: 措辞与 :meth:`VisionExtraction.summary` 那一行**逐字对齐**：「失败」单独出现
#: 会与「空」混起来，两处都写「调用失败」才不会读成两种事实。
_VISION_STATUS_LABEL = {"ok": "成功", "empty": "空", "failed": "调用失败"}

_VISION_STATUS_CLASS = {"empty": "is-empty", "failed": "is-failed"}


def summarize_vision(extraction: Optional[Any]) -> str:
    """视觉解析结局的一行摘要，供预览状态串取用；未走视觉时返回空串。

    与 :func:`summarize_quality` / :func:`summarize_coverage` 的「合格就返回空串」
    **故意不同**：那两个是告警，健康时不打扰；这一行是逐页交代结局的计数
    （spec「视觉解析的结局逐页可查」），全部成功时它本身就是内容而不是噪声。

    未启用视觉时整份文件不走这条路，调用方传进来的是 ``None``，于是那类预览的
    状态串与改动前逐字相同。
    """
    if extraction is None:
        return ""
    return extraction.summary()


def render_vision_block(extraction: Optional[Any]) -> str:
    """渲染逐页视觉结局与「转写字符数 vs 同页文本层字符数」；未走视觉时返回空串。

    **只呈现，不判定**（spec「转写与文本层的对照只呈现不判定」）：表里没有比值、
    没有按比例算出的好坏标记，也不触发任何告警。两个数相差很大是常见形态
    （第 31 页转写 1239 字符、文本层 1439 字符），但那个差说明不了哪一份更准
    ——文本层被 PDF 拆碎，转写可能漏掉表格，各有各的失真方式，判断要人来做。

    ``extraction`` 只用到 ``summary()`` 与 ``page_rows()` 两个方法，所以这里不必
    认识 ``vision`` 模块；未走视觉时传 ``None``，区块不出现。
    """
    if extraction is None:
        return ""

    rows = extraction.page_rows()
    if not rows:
        return ""

    failed = [r for r in rows if r.get("status") == "failed"]
    body: List[str] = []
    for r in rows:
        status = r.get("status", "")
        label = _VISION_STATUS_LABEL.get(status, status)
        cls = _VISION_STATUS_CLASS.get(status, "")
        # 失败页的「说明」以正文来源开头，错误信息跟在后面——只写异常名会让人以为
        # 那一页什么都没有，而它其实有文本层兜底（D8，spec 要求说明正文取自何处）。
        note = ""
        if status == "failed":
            note = "正文取自既有文本层"
            if r.get("error"):
                note += f"；{_escape_html(str(r['error'])[:160])}"
        elif r.get("error"):
            note = _escape_html(str(r["error"])[:160])
        cls_attr = f' class="{cls}"' if cls else ""
        body.append(
            f"<tr{cls_attr}>"
            f'<td>第 {r.get("index", "?")} 页</td>'
            f"<td>{_escape_html(label)}</td>"
            f'<td>{r.get("transcribe_chars", 0):,}</td>'
            f'<td>{r.get("layer_chars", 0):,}</td>'
            f'<td>{r.get("vision_blocks", 0)}</td>'
            f"<td>{note}</td>"
            f"</tr>"
        )

    table_rows = "".join(body)
    summary_html = _escape_html(extraction.summary()).replace("\n", "<br>")
    # 几十页的表会让面板很长，默认折叠；计数与未成功项在摘要行里已经能读到，
    # 展开是为了逐页核对，不是为了第一眼看见。有未成功的页时默认展开——那正是
    # 需要人看一眼的场合。
    open_attr = " open" if failed else ""

    return f"""
        <div class="cv-vision">
            <div class="cv-vision-head">🖼️ 视觉解析</div>
            <p>{summary_html}</p>
            <details{open_attr}>
                <summary>逐页对照（{len(rows)} 页）：转写字符数 vs 同页文本层字符数</summary>
                <table>
                    <thead>
                        <tr>
                            <th>页</th><th>结局</th><th>转写字符</th>
                            <th>文本层字符</th><th>视觉块</th><th>说明</th>
                        </tr>
                    </thead>
                    <tbody>{table_rows}</tbody>
                </table>
                <p class="cv-vision-note">两列字符数只作对照，不据此判定哪一页更好：
                   文本层可能被 PDF 拆碎，转写可能漏掉表格。</p>
            </details>
        </div>
    """


def render_chunk_cards(chunks: Sequence[Dict[str, Any]]) -> str:
    """渲染 chunk 卡片列表。

    纯函数：只依赖传入的 chunk 列表，不碰实例状态。本地预览与从 Qdrant
    回看都调它，所以两条路径的输出必然一致。

    每张卡片展示内容预览、字符数、原文起止位置、标题路径，以及与上一块的
    重叠量——这些是判断切分好坏时真正要看的东西。
    """
    if not chunks:
        return '<div class="cv-empty">⚠️ 没有可展示的 chunk</div>'

    html: List[str] = []
    prev_end = 0

    for i, chunk in enumerate(chunks, start=1):
        content = chunk.get("content", "")
        char_count = len(content)
        heading = chunk.get("heading_path") or ""
        start = chunk.get("start", 0)
        end = chunk.get("end", start + char_count)

        has_overlap = prev_end > 0 and start < prev_end
        overlap_size = prev_end - start if has_overlap else 0

        preview = content[:500]
        if len(content) > 500:
            preview += f"\n\n... (剩余 {len(content) - 500} 字符)"

        html.append(f"""
        <div class="cv-card">
            <div class="cv-card-head">
                <div>
                    <div class="cv-card-no">
                        <span class="cv-idx">{i}</span>
                        <span>Chunk {i} / {len(chunks)}</span>
                    </div>
                    {f'<div class="cv-heading">📂 {_escape_html(heading)}</div>' if heading else ''}
                </div>
                <div class="cv-badges">
                    <span class="cv-badge chars">📏 {char_count} 字符</span>
                    <span class="cv-badge pos">📍 {start}–{end}</span>
                </div>
            </div>
        """)

        if overlap_size > 0:
            pct = int(overlap_size / max(char_count, 1) * 100)
            html.append(f"""
            <div class="cv-overlap-bar">
                🔄 与上一 Chunk 重叠 <strong>{overlap_size}</strong> 字符（约 {pct}%）
            </div>
            """)

        html.append(f'<pre class="cv-body">{_escape_html(preview)}</pre></div>')
        prev_end = end

    return "".join(html)


def render_size_distribution(chunks: Sequence[Dict[str, Any]]) -> str:
    """渲染各 chunk 字符数的分布（纯函数，可直接测试）。

    一眼看出有没有异常碎块或巨块——这类问题在卡片列表里要逐张看数字才发现。
    """
    if not chunks:
        return ""
    sizes = [len(c.get("content", "")) for c in chunks]
    peak = max(sizes) or 1

    bars = []
    for i, s in enumerate(sizes, start=1):
        pct = max(2, int(s / peak * 100))
        bars.append(
            f'<div style="display:flex;align-items:center;gap:8px;margin:2px 0;">'
            f'<span style="width:56px;font-size:11px;color:#a0aec0;">#{i}</span>'
            f'<div style="flex:1;background:#edf2f7;border-radius:3px;height:12px;">'
            f'<div style="width:{pct}%;background:#667eea;height:12px;border-radius:3px;"></div>'
            f"</div>"
            f'<span style="width:64px;font-size:11px;color:#4a5568;text-align:right;">{s} 字符</span>'
            f"</div>"
        )

    return f"""
    <div class="cv-section">
        <h3>📊 Chunk 大小分布</h3>
        <div class="hint">最长 {peak} 字符。柱子过短的说明切得过碎，过长的说明该考虑调小上限。</div>
        {"".join(bars)}
    </div>
    """


def render_distance_curve(
    distances: Sequence[float],
    threshold: float,
    cut_indices: Sequence[int],
    merged_count: int = 0,
) -> str:
    """渲染相邻语义距离曲线，并标出阈值虚线与切点。

    这是语义分割的核心解释图：横轴是原子序号，纵轴是它与下一个原子之间的
    余弦距离。距离越高说明语义转折越明显，虚线是当前敏感度对应的阈值，
    高于虚线的位置就是切点。拖动敏感度滑块时虚线上下移动、切点随之增减，
    「这个参数在干什么」于是变得可见。

    标记只画真正成为 chunk 边界的那几处。``merged_count`` 是被尺寸约束
    （最小块/最大块）合并掉的候选切点数量——不说明这一点，使用者会看到
    曲线上一堆高峰却只有一块，以为滑块没生效。
    """
    n = len(distances)
    if n == 0:
        return ""

    width, height = 900, 220
    pad_l, pad_r, pad_t, pad_b = 48, 16, 16, 32
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    d_min = min(min(distances), threshold)
    d_max = max(max(distances), threshold)
    span = (d_max - d_min) or 1.0

    def x_at(i: int) -> float:
        return pad_l + (i * plot_w / (n - 1) if n > 1 else plot_w / 2)

    def y_at(v: float) -> float:
        return pad_t + plot_h - ((v - d_min) / span) * plot_h

    pts = " ".join(f"{x_at(i):.1f},{y_at(d):.1f}" for i, d in enumerate(distances))
    y_tau = y_at(threshold)

    cuts = set(cut_indices)
    markers = []
    for i in sorted(cuts):
        if 0 <= i < n:
            markers.append(
                f'<circle cx="{x_at(i):.1f}" cy="{y_at(distances[i]):.1f}" r="4.5" '
                f'fill="#e53e3e" stroke="white" stroke-width="1.5"/>'
            )

    # 刻度：y 轴上下界，x 轴首尾
    ticks = [
        f'<text x="{pad_l-8}" y="{y_at(d_max)+4:.1f}" font-size="10" fill="#a0aec0" text-anchor="end">{d_max:.2f}</text>',
        f'<text x="{pad_l-8}" y="{y_at(d_min)+4:.1f}" font-size="10" fill="#a0aec0" text-anchor="end">{d_min:.2f}</text>',
        f'<text x="{pad_l}" y="{height-10}" font-size="10" fill="#a0aec0" text-anchor="middle">原子 1</text>',
        f'<text x="{width-pad_r}" y="{height-10}" font-size="10" fill="#a0aec0" text-anchor="middle">原子 {n+1}</text>',
    ]

    return f"""
    <div class="cv-section">
        <h3>📈 相邻语义距离与切点</h3>
        <div class="hint">
            纵轴是相邻两个文本单元的余弦距离，越高说明语义转折越明显。
            红色虚线是当前敏感度对应的阈值，红点即切点——共 <strong>{len(cuts)}</strong> 处。
            {f"另有 {merged_count} 处候选切点因尺寸约束（最小块/最大块）被合并，未成为实际边界。" if merged_count else ""}
        </div>
        <svg viewBox="0 0 {width} {height}" style="width:100%;height:auto;">
            <line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{pad_t+plot_h}"
                  stroke="#e2e8f0" stroke-width="1"/>
            <line x1="{pad_l}" y1="{pad_t+plot_h}" x2="{width-pad_r}" y2="{pad_t+plot_h}"
                  stroke="#e2e8f0" stroke-width="1"/>
            <line x1="{pad_l}" y1="{y_tau:.1f}" x2="{width-pad_r}" y2="{y_tau:.1f}"
                  stroke="#e53e3e" stroke-width="1.5" stroke-dasharray="6 4"/>
            <text x="{width-pad_r}" y="{y_tau-6:.1f}" font-size="10" fill="#e53e3e"
                  text-anchor="end">阈值 {threshold:.3f}</text>
            <polyline points="{pts}" fill="none" stroke="#667eea" stroke-width="1.8"/>
            {"".join(markers)}
            {"".join(ticks)}
        </svg>
    </div>
    """


def render_chunk_report(
    chunks: Sequence[Dict[str, Any]],
    diagnostics: Optional[Dict[str, Any]] = None,
    title: str = "文档切分可视化",
    doc_name: str = "",
    strategy_label: str = "",
    params: Optional[Dict[str, Any]] = None,
    param_specs: Optional[Sequence[Dict[str, Any]]] = None,
    extra_params_html: str = "",
    coverage: Optional[Dict[str, Any]] = None,
    quality: Optional[Dict[str, Any]] = None,
    vision: Optional[Any] = None,
    stored: bool = False,
) -> str:
    """渲染完整的切分报告：统计 + (质量告警) + (视觉解析) + 参数 + (语义分析) + 卡片列表。

    ``diagnostics`` 决定要不要渲染语义分析区块。只有语义分割会填它，所以
    界面不需要按策略写分支——段落分割传空字典，曲线和分布自然就不出现。

    ``coverage`` 透传给 :func:`render_stats`，用于在统计行里多出一格内容覆盖率；
    预览路径由调用方算好传入，回看路径拿不到提取文本，故默认为 None（该格不出现）。

    ``quality`` 是提取质量报告，``stored=True`` 表示它来自**已存 chunk** 而非本次
    提取——两者的告警文案不同（见 :func:`render_quality_block`）。判定合格时两者
    都不渲染任何东西。

    ``vision`` 是逐页视觉结局（``vision.VisionExtraction``，回看路径没有故为
    ``None``）。它与质量报告相反：不成功时不渲染，成功时**照样**渲染——计数与
    逐页字符对照是要求呈现的内容，不是告警（见 :func:`render_vision_block`）。
    """
    diagnostics = diagnostics or {}
    parts = [_CSS, '<div class="cv-container">']

    parts.append(f"""
        <div class="cv-header">
            <div class="doc-name">🔪 {_escape_html(title)}</div>
            <h2>📋 {_escape_html(doc_name or "(未命名文档)")}</h2>
            {render_stats(chunks, coverage)}
        </div>
    """)

    parts.append(render_quality_block(quality, stored=stored))
    parts.append(render_vision_block(vision))

    if params is not None and param_specs is not None:
        parts.append(render_param_bar(strategy_label, params, param_specs))
    elif extra_params_html:
        parts.append(extra_params_html)

    # 语义分析区块：diagnostics 为空时不渲染
    if diagnostics.get("distances") is not None:
        parts.append(render_distance_curve(
            diagnostics.get("distances", []),
            diagnostics.get("threshold", 0.0),
            diagnostics.get("cut_indices", []),
            merged_count=diagnostics.get("merged_cut_count", 0),
        ))
        parts.append(render_size_distribution(chunks))

    parts.append(render_chunk_cards(chunks))
    parts.append("</div>")

    return "".join(parts)
