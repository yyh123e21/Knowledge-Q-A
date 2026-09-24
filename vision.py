#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视觉提取：把页面（与独立图片）渲染后交给视觉模型，产出可切分的文本。

职责边界（design.md D1）：本模块只回答「某个文件 -> 每页的视觉文本与状态」。
它不切分、不写知识库、不 import ``11_Q&A_Assistant.py``。把视觉文本合并进提取
文本（T′）这一步在 ``chunking.py::_extract_record`` 里做。

为什么要落盘（design.md D4）：一次提取是分钟级的真实 API 调用，而通路本身会断
——实测遇到过本机服务被大图打崩、云端中转抛 ``ssl.SSLEOFError``、一次调用重试
四次才成功。没有逐页 checkpoint，一次中断就要从头再付一遍。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# ===========================================================================
# 1. 常量
# ===========================================================================

#: 整页渲染比例。1.5 是实测的准确率拐点，不是折中值（design.md D3）：
#: 同一句密集中文在 0.03 MP 下 235B 会读错字，1.5 与 3.0 下逐字正确，
#: 而耗时随像素线性增长，所以不往上取。
PAGE_SCALE = 1.5

#: 落盘缓存目录名（相对本模块所在目录）。
CACHE_DIRNAME = ".chunk_cache"

#: 视觉块的成对标记（design.md D6）。选 ASCII 成对标记是因为它必须能被正则
#: 稳定识别，且在中文原文里不可能自然出现。
VISION_OPEN = "[[VISION"
VISION_CLOSE = "[[/VISION]]"
VISION_OPEN_RE = re.compile(r"^\[\[VISION(?P<label>[^\]]*)\]\]\s*$")
VISION_CLOSE_RE = re.compile(r"^\[\[/VISION\]\]\s*$")

#: 每页最多保留的输出长度，防止模型复读时把整份文档撑爆。
MAX_TOKENS = 4096

#: 逐页重试。重试发生在**页**这一层，不是整份文档那层（design.md D11）——
#: 后者会让一次失败重跑整本 70 页。
RETRY_ATTEMPTS = 4
RETRY_BASE_DELAY = 2.0

#: 单页超时（秒）。取自实测（tasks.md 2.9）：6 页样本 9.8 / 14.7 / 23.4 / 24.2 / 33.5 /
#: 60.8 秒，中位 23.8、最长 60.8。取 180 是实测最长的 3.0 倍，因为页间方差有 6 倍
#: （最长 60.8 vs 最短 9.8），固定倍数宁可宽一点也不宜卡边。两侧代价不对称：
#: 定低了会把本来能成的页判成失败，那一页就只剩空格拆碎的文本层兜底；定高了只是
#: 在真正挂起时多等几分钟，而且一次运行里最多每页发生一次。
#: 单页最坏总耗时（含重试）= 180 × 4 = 12 分钟。
PAGE_TIMEOUT = 180.0

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_FAILED = "failed"

#: 视觉模型能处理的格式。PDF 走逐页渲染，其余按单张图处理。
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp", ".gif"}

_VLM_VARS = ("VLM_BASE_URL", "VLM_MODEL_ID", "VLM_API_KEY")

#: 转写提示词。四条要求逐一对应 spec 里的可判定断言：
#: 1 保留标题层级 -> 「Markdown 结构感知分割」仍能给出标题路径
#: 2 图号题注原样保留 -> 「按图号检索命中」
#: 3 看不清就说看不清 -> 「图上的数值不来自推测」
#: 4 不要解释性包装 -> 产出可直接切分
PROMPT = """你是文档页面转写器。给你一张书页或论文页面的图片，请输出这一页的 markdown 文本。

要求：

1. 逐字转写页面上的正文，保留原有的标题层级（用 #、##、### 表示）、段落与列表结构。
   不要总结、不要改写、不要补充页面上没有的内容。公式用 LaTeX 原样写出。

2. 页面上的每一张图、每一个表，在它出现的位置输出一段描述，并用下面这对标记包起来。
   开标记要和题注写在**同一行**，并以 `]]` 结尾；闭标记要**单独占一行**，前后不要再写字：

[[VISION 图号或表号与题注，若页面上有]]
（在这里写描述，可以写多行）
[[/VISION]]

   图号与题注必须原样抄下来（例如「图 2-7 收敛性能分析」），不要改写编号。
   描述里要写清：图的类型（框图/曲线图/柱状图/示意图）、坐标轴名称与刻度值、图例、
   曲线或数据的走势、以及图中出现的全部文字标签。表格要逐行写出单元格内容。

3. 只写你在图上真正看得清的内容。数值看不清就如实写「不可精确辨识」，
   绝对不要猜一个数填进去当作图上的读数。

4. 不要输出任何解释性的话，也不要用代码块把你自己的输出包起来。
"""


# ===========================================================================
# 2. 配置与指纹
# ===========================================================================

class VisionUnavailableError(RuntimeError):
    """视觉提取不可用：配置缺失。消息里带上缺的是哪一项。"""


def missing_config_names() -> List[str]:
    """返回未设置的 ``VLM_*`` 变量名。**只返回变量名，不返回值**（禁区 7）。"""
    return [name for name in _VLM_VARS if not (os.environ.get(name) or "").strip()]


def is_available() -> bool:
    """三项 ``VLM_*`` 都在时才算可用。"""
    return not missing_config_names()


@dataclass(frozen=True)
class VisionConfig:
    """一次视觉提取的配置。指纹进缓存键，所以**不含 api_key**（design.md D4）。"""

    base_url: str
    model: str
    api_key: str
    scale: float = PAGE_SCALE
    timeout: float = PAGE_TIMEOUT

    @property
    def fingerprint(self) -> str:
        """配置指纹 = base_url + 模型 + 提示词哈希 + 渲染比例。

        四项缺一不可：少任何一项，改了那一项之后就会静默复用旧配置的产物，
        产出「看起来正常但属于上一套配置」的文本——本设计里最隐蔽的一类失败。
        """
        raw = "|".join(
            [
                self.base_url,
                self.model,
                hashlib.sha256(PROMPT.encode("utf-8")).hexdigest(),
                f"{self.scale:g}",
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def describe(self) -> Dict[str, Any]:
        """给 meta.json 与预览用的说明。**故意不含 api_key**。"""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "scale": self.scale,
            "prompt_sha256": hashlib.sha256(PROMPT.encode("utf-8")).hexdigest()[:16],
            "fingerprint": self.fingerprint,
        }


def load_config() -> Optional[VisionConfig]:
    """读 ``.env`` 的三项 ``VLM_*``；缺任一项返回 ``None``。"""
    if not is_available():
        return None
    return VisionConfig(
        base_url=os.environ["VLM_BASE_URL"].strip(),
        model=os.environ["VLM_MODEL_ID"].strip(),
        api_key=os.environ["VLM_API_KEY"].strip(),
    )


def is_visual_format(path: str) -> bool:
    """PDF 与图片走视觉提取；其余格式不进视觉段（proposal Non-goals 2）。"""
    ext = (os.path.splitext(path)[1] or "").lower()
    return ext == ".pdf" or ext in _IMAGE_EXTS


def is_pdf(path: str) -> bool:
    return (os.path.splitext(path)[1] or "").lower() == ".pdf"


# ===========================================================================
# 3. 渲染与逐页文本层
# ===========================================================================

@dataclass
class PageImage:
    """一页（或一张图）的渲染结果，连同该页的文本层。"""

    index: int          # 1 起
    png: bytes
    width: int
    height: int
    layer_text: str     # 该页的文本层；独立图片没有，为空串


def _png_from_pil(pil) -> bytes:
    buf = io.BytesIO()
    pil.save(buf, format="PNG")
    return buf.getvalue()


def _render_pdf(path: str, scale: float) -> List[PageImage]:
    import pypdfium2 as pdfium

    pages: List[PageImage] = []
    doc = pdfium.PdfDocument(path)
    try:
        total = len(doc)
        for i in range(total):
            page = doc[i]
            try:
                bitmap = page.render(scale=scale)
                pil = bitmap.to_pil().convert("RGB")
                try:
                    layer = page.get_textpage().get_text_range() or ""
                except Exception:
                    # 文本层坏掉不该让整页渲染失败——对照数字退化为 0 即可，
                    # 该页仍然会被转写。
                    layer = ""
                pages.append(
                    PageImage(
                        index=i + 1,
                        png=_png_from_pil(pil),
                        width=pil.width,
                        height=pil.height,
                        layer_text=layer,
                    )
                )
            finally:
                page.close()
    finally:
        doc.close()
    return pages


def _render_image(path: str) -> List[PageImage]:
    from PIL import Image

    with Image.open(path) as im:
        pil = im.convert("RGB")
        return [
            PageImage(
                index=1,
                png=_png_from_pil(pil),
                width=pil.width,
                height=pil.height,
                layer_text="",
            )
        ]


def render_pages(path: str, scale: float = PAGE_SCALE) -> List[PageImage]:
    """把文件渲染成逐页位图，并附带每一页的文本层。

    独立图片文件是单页（index=1），没有文本层。
    """
    if is_pdf(path):
        return _render_pdf(path, scale)
    return _render_image(path)


def page_count(path: str) -> int:
    """只问页数，不渲染。"""
    if not is_pdf(path):
        return 1
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(path)
    try:
        return len(doc)
    finally:
        doc.close()


# ===========================================================================
# 4. 调用视觉模型
# ===========================================================================

def _call_model(cfg: VisionConfig, image: PageImage, timeout: float) -> str:
    """单次调用。重试不在这里做——见 ``transcribe_page``。

    ``max_retries=0`` 是刻意的：SDK 自己的重试发生在单次请求内部，与本设计的
    「按页 checkpoint、按页重试」语义冲突，会让一次失败既重试又不可观测。
    """
    from openai import OpenAI

    client = OpenAI(
        base_url=cfg.base_url, api_key=cfg.api_key, timeout=timeout, max_retries=0
    )
    b64 = base64.b64encode(image.png).decode("ascii")
    resp = client.chat.completions.create(
        model=cfg.model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            }
        ],
        max_tokens=MAX_TOKENS,
        temperature=0,
    )
    content = resp.choices[0].message.content
    return content or ""


def transcribe_page(
    cfg: VisionConfig,
    image: PageImage,
    *,
    caller: Optional[Callable[[VisionConfig, PageImage, float], str]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    attempts: int = RETRY_ATTEMPTS,
) -> Tuple[str, int]:
    """转写一页，带指数退避重试。

    ``caller`` 与 ``sleep`` 可注入，供验证替换（tasks.md 2.4）。

    Returns:
        ``(文本, 实际尝试次数)``

    Raises:
        Exception: 重试耗尽后把最后一次异常抛出去。
    """
    call = caller or _call_model
    nap = sleep or time.sleep
    delays: List[float] = []

    last: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return call(cfg, image, cfg.timeout), attempt
        except BaseException as e:  # noqa: BLE001 - 重试要吃掉所有通路故障
            last = e
            if attempt < attempts:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                delays.append(delay)
                nap(delay)
    assert last is not None
    raise last


# ===========================================================================
# 5. 标记规整
# ===========================================================================

def normalize_vision_markers(text: str) -> str:
    """把模型给的标记规整成「成对、独占一行」。

    这一步不是可有可无的收尾。实测第 31 页时，模型写出来的是:

        [[VISION 图 2-5 MLRA 算法框架图      <- 开标记，本行没有 ]]
        该图为框图，……                        <- 描述
        ]]                                     <- 闭标记是单独一行的 ]]
        图 2-5 MLRA 算法框架图                  <- 又把题注重复了一遍

    与提示词要求的成对形态不一致（提示词已按这个实测结果收紧一次，但模型输出
    本来就不该指望）。若原样放行，``iter_vision_blocks`` 会认定这一页「0 个视觉块」，
    D6 的保护区间随之失效——**视觉描述会被切分器切开，且不报任何错**。
    所以这里把观察到的所有变体都收敛成一种形态，下游只需认这一种。

    接受的变体：
      * 开标记：``[[VISION 题注]]`` / ``[[VISION 题注``（缺右括号）/ ``[[VISION]]``
      * 闭标记：``[[/VISION]]`` / 单独一行的 ``]]`` / 描述同行收尾的 ``]]``

    未闭合的开标记在末尾补上闭标记；没有开标记的闭标记被丢弃（其前的正文保留）。
    """
    out: List[str] = []
    is_open = False

    for raw in text.splitlines():
        stripped = raw.strip()

        # 开标记：只要行首是 [[VISION 就算，不要求本行有 ]]
        if stripped.startswith(VISION_OPEN):
            if is_open:
                out.append(VISION_CLOSE)  # 上一个没关，先替它关上
            label = stripped[len(VISION_OPEN):]
            if label.endswith("]]"):
                label = label[:-2]
            label = label.strip()
            out.append(f"{VISION_OPEN} {label}]]" if label else f"{VISION_OPEN}]]")
            is_open = True
            continue

        # 闭标记之一：[[/VISION]]，可能和正文挤在同一行
        if VISION_CLOSE in stripped:
            head = stripped.split(VISION_CLOSE, 1)[0].strip()
            if is_open:
                if head:
                    out.append(head)
                out.append(VISION_CLOSE)
                is_open = False
            elif head:
                out.append(head)  # 无主的闭标记：不吞掉它前面的正文
            continue

        if is_open and stripped == "]]":
            out.append(VISION_CLOSE)
            is_open = False
            continue

        if is_open and stripped.endswith("]]"):
            head = stripped[:-2].strip()
            if head:
                out.append(head)
            out.append(VISION_CLOSE)
            is_open = False
            continue

        out.append(raw.rstrip())

    if is_open:
        out.append(VISION_CLOSE)

    return "\n".join(out).strip()


def iter_vision_blocks(text: str) -> List[Tuple[int, int]]:
    """列出 ``text`` 里视觉块的 ``(start, end)`` 字符区间（含两端标记）。

    只看独占一行的成对标记——这正是 ``normalize_vision_markers`` 保证的形态。
    """
    blocks: List[Tuple[int, int]] = []
    pos = 0
    start: Optional[int] = None

    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if start is None and VISION_OPEN_RE.match(stripped):
            start = pos
        elif start is not None and VISION_CLOSE_RE.match(stripped):
            blocks.append((start, pos + len(line)))
            start = None
        pos += len(line)

    return blocks


def count_vision_blocks(text: str) -> int:
    return len(iter_vision_blocks(text))


# ===========================================================================
# 6. 落盘缓存
# ===========================================================================

def cache_root() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CACHE_DIRNAME)


def file_fingerprint(path: str) -> str:
    """文件指纹 = 绝对路径 + 大小 + mtime。文件被换掉则指纹变。"""
    st = os.stat(path)
    raw = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _doc_cache_dir(path: str) -> str:
    """一份文件一个目录。``meta.json`` 放这里，记最近一次运行用的配置。"""
    return os.path.join(cache_root(), file_fingerprint(path))


def _config_dir(path: str, cfg: "VisionConfig") -> str:
    """再按配置指纹分一层：``.chunk_cache/<文件指纹>/<配置指纹>/page-NNNN.json``。

    为什么不把两套配置的逐页结果平铺在同一个目录里、只靠 JSON 里的
    ``config_fingerprint`` 判别：那样换模型重跑会**覆盖**上一次的产物，
    换回来时 70 页要重付一遍（实测：20 分钟、70 次真实调用）。
    而「比较不同模型哪个读得准」正是把模型做成可配置的原因——这个来回
    是最该省下的花费，不是边缘场景。多出来的磁盘占用只是文本，可忽略。
    """
    return os.path.join(_doc_cache_dir(path), cfg.fingerprint)


def _page_path(path: str, index: int, cfg: "VisionConfig") -> str:
    return os.path.join(_config_dir(path, cfg), f"page-{index:04d}.json")


def _meta_path(path: str) -> str:
    return os.path.join(_doc_cache_dir(path), "meta.json")


def read_meta(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(_meta_path(path), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_meta(path: str, cfg: VisionConfig, total_pages: int) -> None:
    os.makedirs(_doc_cache_dir(path), exist_ok=True)
    os.makedirs(_config_dir(path, cfg), exist_ok=True)
    meta = {
        "file": {"path": os.path.abspath(path), "fingerprint": file_fingerprint(path)},
        "config": cfg.describe(),
        "pages": total_pages,
    }
    with open(_meta_path(path), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def read_cached_page(path: str, index: int, cfg: VisionConfig) -> Optional[Dict[str, Any]]:
    """读一页的缓存。配置指纹不符就当作没缓存（design.md D4）。"""
    p = _page_path(path, index, cfg)
    try:
        with open(p, "r", encoding="utf-8") as f:
            rec = json.load(f)
    except Exception:
        return None
    if rec.get("config_fingerprint") != cfg.fingerprint:
        return None
    return rec


def write_cached_page(
    path: str, index: int, cfg: VisionConfig, rec: Dict[str, Any]
) -> None:
    """写一页的缓存。**失败的页不写**——它应当在下次运行时被重试。

    写失败必须报错，不能静默退化成「不缓存」：那会让每次重复预览都重付一次
    API 费用，而使用者以为命中了缓存（design.md 风险 8）。
    """
    os.makedirs(_config_dir(path, cfg), exist_ok=True)
    payload = {
        "index": index,
        "status": rec.get("status", STATUS_OK),
        "text": rec.get("text", ""),
        "layer_chars": rec.get("layer_chars", 0),
        "error": rec.get("error", ""),
        "config_fingerprint": cfg.fingerprint,
    }
    with open(_page_path(path, index, cfg), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# ===========================================================================
# 7. 主入口
# ===========================================================================

@dataclass
class PageResult:
    """一页的视觉提取结局。"""

    index: int
    status: str            # ok | empty | failed
    text: str              # 规整后的转写（含视觉块标记）；失败页为文本层
    layer_chars: int = 0
    error: str = ""
    from_cache: bool = False

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def vision_blocks(self) -> int:
        return count_vision_blocks(self.text)

    @property
    def is_fallback(self) -> bool:
        """正文是否取自文本层（即视觉解析没成功）。"""
        return self.status == STATUS_FAILED


@dataclass
class VisionExtraction:
    """一份文件的视觉提取结果。"""

    pages: List[PageResult] = field(default_factory=list)
    text: str = ""
    config: Optional[Dict[str, Any]] = None
    calls: int = 0
    cache_hits: int = 0

    @property
    def total(self) -> int:
        return len(self.pages)

    def counts(self) -> Dict[str, int]:
        c = {STATUS_OK: 0, STATUS_EMPTY: 0, STATUS_FAILED: 0}
        for p in self.pages:
            c[p.status] = c.get(p.status, 0) + 1
        return c

    def failed_pages(self) -> List[int]:
        return [p.index for p in self.pages if p.status == STATUS_FAILED]

    def empty_pages(self) -> List[int]:
        return [p.index for p in self.pages if p.status == STATUS_EMPTY]

    def fallback_pages(self) -> List[int]:
        return [p.index for p in self.pages if p.is_fallback]

    def unsuccessful_pages(self) -> List[int]:
        """未成功的页：**空结果与调用失败都算**（spec「空结果计为未成功」）。

        与 :meth:`failed_pages` 分开是因为两者的正文来源不同：失败页回退到该页
        文本层，空结果页什么都没产出、该页内容不进提取结果。状态串必须分别说清，
        合在一句里就会把「这一页有兜底文字」说成「这一页没有内容」或反之。
        """
        return [p.index for p in self.pages if p.status != STATUS_OK]

    def summary(self) -> str:
        """给预览用的一行摘要。形状对齐 spec 的例子
        「视觉解析：67 成功 / 1 空 / 2 失败」。合格时不多说。

        有未成功的页时**必须列出它们的页码并说明正文取自何处**（spec
        「视觉解析的结局逐页可查」的两个 scenario）——所以这一行不是告警，
        而是要求呈现的内容，全部成功时也照样给计数。
        """
        c = self.counts()
        unit = "页" if len(self.pages) > 1 or self.total != 1 else "张"
        parts = [f"{c[STATUS_OK]} 成功"]
        if c[STATUS_EMPTY]:
            parts.append(f"{c[STATUS_EMPTY]} 空")
        if c[STATUS_FAILED]:
            parts.append(f"{c[STATUS_FAILED]} 失败")
        line = f"视觉解析：{self.total} {unit} —— " + " / ".join(parts)

        page_word = "页" if unit == "页" else "张"
        bad = [p for p in self.pages if p.status != STATUS_OK]
        if bad:
            shown = "、".join(
                f"第 {p.index} {page_word}"
                f"（{'空' if p.status == STATUS_EMPTY else '调用失败'}）"
                for p in bad[:10]
            )
            if len(bad) > 10:
                shown += f" 等 {len(bad)} 处"
            line += f"\n   ⚠️ 未成功 {len(bad)} 处：{shown}"
            if self.failed_pages():
                line += f"\n      · 调用失败的{page_word}：正文取自既有文本层，未经视觉处理"
            if self.empty_pages():
                line += (
                    f"\n      · 空结果的{page_word}：模型未返回任何文字，"
                    f"该{page_word}的内容未进入提取结果"
                )
        if self.cache_hits:
            line += f"\n   ℹ️ 其中 {self.cache_hits} 页取自落盘缓存，未再次调用模型"
        return line

    def page_rows(self) -> List[Dict[str, Any]]:
        """逐页对照：转写字符数 vs 同页文本层字符数（只呈现，不判定，D7）。"""
        return [
            {
                "index": p.index,
                "status": p.status,
                "transcribe_chars": p.chars,
                "layer_chars": p.layer_chars,
                "vision_blocks": p.vision_blocks,
                "error": p.error,
            }
            for p in self.pages
        ]


def extract(
    path: str,
    *,
    config: Optional[VisionConfig] = None,
    caller: Optional[Callable[[VisionConfig, PageImage, float], str]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    pages: Optional[List[PageImage]] = None,
) -> VisionExtraction:
    """提取一份文件的视觉文本。

    Raises:
        VisionUnavailableError: ``VLM_*`` 配置缺失。
        Exception: 渲染失败时原样抛出。
    """
    cfg = config or load_config()
    if cfg is None:
        missing = "、".join(missing_config_names()) or "VLM_*"
        raise VisionUnavailableError(
            f"视觉提取不可用：.env 缺少配置 {missing}。"
            f"补齐后重新预览；在此之前图片文件无法提取内容。"
        )

    if pages is None:
        pages = render_pages(path, cfg.scale)

    write_meta(path, cfg, len(pages))

    results: List[PageResult] = []
    extraction = VisionExtraction(config=cfg.describe())

    for image in pages:
        cached = read_cached_page(path, image.index, cfg)
        if cached is not None:
            extraction.cache_hits += 1
            results.append(
                PageResult(
                    index=image.index,
                    status=cached.get("status", STATUS_OK),
                    text=cached.get("text", ""),
                    layer_chars=cached.get("layer_chars", len(image.layer_text)),
                    error=cached.get("error", ""),
                    from_cache=True,
                )
            )
            continue

        extraction.calls += 1
        try:
            raw, _attempts = transcribe_page(cfg, image, caller=caller, sleep=sleep)
        except BaseException as e:  # noqa: BLE001 - 失败要落成状态，不能中断整份文档
            results.append(
                PageResult(
                    index=image.index,
                    status=STATUS_FAILED,
                    text=image.layer_text,          # 回退到该页文本层（D8）
                    layer_chars=len(image.layer_text),
                    error=f"{type(e).__name__}: {e}",
                )
            )
            continue

        text = normalize_vision_markers(raw)
        status = STATUS_OK if text.strip() else STATUS_EMPTY
        rec = {
            "status": status,
            "text": text,
            "layer_chars": len(image.layer_text),
            "error": "",
        }
        # 失败的页不落盘（它要在下次运行时被重试）；空结果也落盘——重问一次
        # 既要再付一次钱，也未必能得到同样的回答（见下）。
        #
        # 早期这里写的理由是「temperature=0 下的稳定回答」，实测不成立：同一个
        # 模型、同一页、temperature=0，两次调用给出的是不同的文本——3.1 那次
        # 并发跑了两份（同一个缓存目录），同一页的转写长度差了 5–73 字符，
        # 带视觉块的页数一份是 26、另一份是 27。MoE 模型的专家路由与批量相关的
        # 算子顺序都会引入这种抖动。**因此落盘缓存不只是省钱的优化，它是
        # 「同一份文件得到同一份 T′」的唯一保证。**
        write_cached_page(path, image.index, cfg, rec)
        results.append(
            PageResult(
                index=image.index,
                status=status,
                text=text,
                layer_chars=len(image.layer_text),
            )
        )

    extraction.pages = results
    extraction.text = "\n\n".join(p.text for p in results if p.text.strip())
    return extraction
