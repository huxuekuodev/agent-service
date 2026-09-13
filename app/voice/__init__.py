"""语音通话能力：浏览器识别 → agent → **服务端按句流式 TTS**。

    audio.py     播报文本清洗 + 分句切块（长文本不整段合成）
    service.py   TTS（OpenAI 兼容 /audio/speech，当前硅基 CosyVoice2）

为什么服务端只做 TTS：浏览器原生语音识别（Web Speech API）零成本、零延迟；
而实测服务端 ASR（硅基 SenseVoiceSmall）3 秒音频要 45 秒，不适合通话。
详见 ``docs/语音通话方案.md``。
"""

from app.voice.audio import clean_for_speech, plan_review_speech, split_for_speech
from app.voice.service import SpeechChunk, VoiceError, VoiceService, get_voice_service

__all__ = [
    "SpeechChunk",
    "VoiceError",
    "VoiceService",
    "clean_for_speech",
    "get_voice_service",
    "plan_review_speech",
    "split_for_speech",
]
