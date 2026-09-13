"""语音播报文本处理单元测试（离线：清洗 + 分句切块）。"""

from __future__ import annotations

from app.voice.audio import clean_for_speech, plan_review_speech, split_for_speech

# --------------------------------------------------------------------------- 清洗


def test_clean_strips_markdown_noise() -> None:
    text = """## 天气结论

北京今天**晴**，最高 `29℃`。

```json
{"temp": 29}
```

| 城市 | 温度 |
| --- | --- |
| 北京 | 29 |
"""
    cleaned = clean_for_speech(text)
    assert "**" not in cleaned and "`" not in cleaned and "#" not in cleaned
    assert "json" not in cleaned and "temp" not in cleaned  # 代码块不朗读
    assert "---" not in cleaned
    assert "北京" in cleaned and "29℃" in cleaned


def test_clean_handles_list_markers_and_empty() -> None:
    assert clean_for_speech("- 第一点\n- 第二点") == "第一点 第二点"
    assert clean_for_speech("1. 甲\n2. 乙") == "甲 乙"
    assert clean_for_speech("") == ""
    assert clean_for_speech("   \n\n  ") == ""


# --------------------------------------------------------------------------- 分句


def test_first_chunk_is_short_for_fast_first_sound() -> None:
    long_answer = "北京今天白天晴，最高气温二十九度，最低气温十五度，北风一到三级，适合外出活动。明天多云转阴，气温略有下降。"
    chunks = split_for_speech(long_answer, max_chars=60, min_chars=12, first_max_chars=30)
    assert len(chunks) >= 2
    assert len(chunks[0]) <= 30, f"首块应更短: {chunks[0]}"
    # 内容不丢失（首块 + 其余拼起来覆盖原文要点）
    assert "北京今天" in chunks[0]
    assert "明天" in "".join(chunks[1:])


def test_short_sentences_are_merged() -> None:
    """过短的句子不该各自成块（否则一顿一顿）。"""
    chunks = split_for_speech("好的。明天晴。后天雨。", min_chars=6, max_chars=60)
    assert len(chunks) <= 2, chunks


def test_long_sentence_is_split_without_losing_text() -> None:
    one_long = "北京今天白天晴最高气温二十九度最低气温十五度北风一到三级适合外出活动明天多云转阴气温略降最高二十六度最低十四度早晚温差大建议带外套" * 2
    chunks = split_for_speech(one_long, max_chars=50, min_chars=10)
    assert all(len(c) <= 50 for c in chunks), [len(c) for c in chunks]
    assert sum(len(c) for c in chunks) == len(one_long)


def test_max_chunks_caps_and_notes() -> None:
    text = "。".join(f"第{i}句话内容" for i in range(1, 40))
    chunks = split_for_speech(text, max_chars=20, min_chars=4, max_chunks=3)
    assert len(chunks) == 3
    assert "界 面 文 字" not in chunks[-1]  # 只做提示拼接，不打散字符
    assert "界面文字" in chunks[-1]


def test_empty_input_returns_no_chunks() -> None:
    assert split_for_speech("") == []
    assert split_for_speech("```\ncode only\n```") == []


def test_split_respects_punctuation_boundaries() -> None:
    chunks = split_for_speech("第一句结束。第二句结束！第三句结束？", max_chars=10, min_chars=4, first_max_chars=10)
    assert all(c.endswith(("。", "！", "？")) or c == "" for c in chunks), chunks


# --------------------------------------------------------------------------- 计划确认播报


def test_plan_review_speech_reads_steps_and_prompt() -> None:
    speech = plan_review_speech({"items": [{"name": "查询北京天气"}, {"name": "汇总结果"}]})
    assert "查询北京天气" in speech and "汇总结果" in speech
    assert "继续" in speech


def test_plan_review_speech_without_items_still_prompts() -> None:
    speech = plan_review_speech({})
    assert "计划已生成" in speech and "继续" in speech
