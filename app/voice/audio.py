"""语音播报文本处理：清洗 + **分句切块**（长文本不整段合成，避免用户干等）。

为什么分句：TTS 接口是"整段请求 → 整段返回"，实测延迟随文本线性增长
（硅基 CosyVoice2：20 字 0.53s / 45 字 0.93s / 90 字 1.48s）。整段合成意味着用户要等到
**全部**合成完才听到第一句；按句切块后，第一句合成完就能播，后续句子边播边合成。

切块规则：
  - 先去 Markdown 噪音（标题符/强调符/代码块/表格线）——口语不该念这些；
  - 按句末标点（。！？；!?;）切句，过短的句子并到下一句（避免一句一顿的机械感）；
  - **首块更短**（``first_max_chars``）：让第一声尽快响起，这是通话体感的关键；
  - 单句超长时按逗号/顿号再切，仍超长则按字符硬切（保护单次请求时长）；
  - 总块数封顶（``max_chunks``），超出部分丢弃并提示（完整文本仍在界面上）。
"""

from __future__ import annotations

import re

__all__ = ["clean_for_speech", "plan_review_speech", "split_for_speech"]

#: 句末标点（用于切句，保留标点本身）
_SENTENCE_END = re.compile(r"(?<=[。！？；!?;])\s*")
#: 次级停顿（把过长的句子再切短）
_COMMA = re.compile(r"(?<=[，、,])")
#: Markdown 强调/代码标记
_MD_NOISE = re.compile(r"(\*\*|__|`{1,3}|~~)")
#: 纯分隔行/表格线
_SEPARATOR_LINE = re.compile(r"^\s*[-|:\s]{3,}\s*$")


def clean_for_speech(text: str) -> str:
    """把 Markdown 答复清洗成适合朗读的纯文本。"""
    if not text:
        return ""
    lines: list[str] = []
    in_code_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block or not line or _SEPARATOR_LINE.match(line):
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)  # 标题符
        line = re.sub(r"^[-*+•]\s+", "", line)  # 无序列表符
        line = re.sub(r"^\d+[.、)]\s+", "", line)  # 有序列表符
        line = _MD_NOISE.sub("", line)
        line = line.replace("|", "，")  # 表格竖线当作停顿
        if line:
            lines.append(line)
    return " ".join(" ".join(lines).split())


def _hard_split(piece: str, limit: int) -> list[str]:
    """按字符数硬切（没有标点可用时的兜底）。"""
    return [piece[i : i + limit] for i in range(0, len(piece), limit)]


def _split_long(piece: str, limit: int) -> list[str]:
    """把超长句按逗号再切，仍超长则硬切。"""
    if len(piece) <= limit:
        return [piece]
    parts: list[str] = []
    buffer = ""
    for segment in _COMMA.split(piece):
        if not segment:
            continue
        if len(buffer) + len(segment) <= limit:
            buffer += segment
        elif buffer:
            parts.append(buffer)
            buffer = segment
        else:
            buffer = segment
        if len(buffer) > limit:  # 单段就超长（中途没有标点）：硬切
            parts.extend(_hard_split(buffer, limit))
            buffer = ""
    if buffer:
        parts.append(buffer)
    return parts or _hard_split(piece, limit)


def split_for_speech(
    text: str,
    *,
    max_chars: int = 60,
    min_chars: int = 12,
    first_max_chars: int = 30,
    max_chunks: int = 20,
) -> list[str]:
    """把答复切成若干"可依次合成并播放"的小块。

    Args:
        text: 原始答复（任意 Markdown）。
        max_chars: 常规单块上限。
        min_chars: 单块下限（更短的句子会与下一句合并，避免一顿一顿）。
        first_max_chars: 首块上限（更短 → 第一声更快）。
        max_chunks: 最多切几块（超出丢弃，界面仍有完整文字）。

    Returns:
        文本块列表；无有效内容时返回空列表。
    """
    cleaned = clean_for_speech(text)
    if not cleaned:
        return []

    pieces: list[str] = []
    for sentence in _SENTENCE_END.split(cleaned):
        sentence = sentence.strip()
        if not sentence:
            continue
        pieces.extend(_split_long(sentence, max_chars))

    # 首块优先短：首句如果超过 first_max_chars，先在逗号处切一刀（第一声更快响起）
    if pieces and len(pieces[0]) > first_max_chars:
        head = _split_long(pieces[0], first_max_chars)
        pieces = [*head, *pieces[1:]]

    chunks: list[str] = []
    buffer = ""
    for piece in pieces:
        limit = first_max_chars if not chunks else max_chars
        if buffer and len(buffer) >= min_chars and len(buffer) + len(piece) > limit:
            chunks.append(buffer)
            buffer = piece
        else:
            buffer = f"{buffer}{piece}" if buffer else piece
        # 首块攒够就先出去，让第一声尽快响
        if not chunks and len(buffer) >= first_max_chars:
            chunks.append(buffer)
            buffer = ""
    if buffer:
        chunks.append(buffer)

    if len(chunks) > max_chunks:
        kept = chunks[:max_chunks]
        kept[-1] = f"{kept[-1]}（内容较长，其余请看界面文字）"
        return kept
    return chunks


def plan_review_speech(payload: dict, *, max_items: int = 4) -> str:
    """把计划确认卡片转成一句适合朗读的话（语音模式下的 interrupt 播报）。"""
    items = (payload or {}).get("items") or []
    names = [str(it.get("name") or it.get("plan_id") or "") for it in items[:max_items]]
    steps = "，".join(n for n in names if n)
    if not steps:
        return "计划已生成。需要调整就直接说，否则说「继续」开始执行。"
    more = f"等 {len(items)} 步" if len(items) > max_items else ""
    return f"计划已生成{more}：{steps}。需要调整就直接说，否则说「继续」开始执行。"
