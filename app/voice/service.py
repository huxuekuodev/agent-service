"""语音合成服务（TTS）：硅基流动 CosyVoice2，按句流式返回。

链路里**只有 TTS 在服务端**：语音识别用浏览器原生能力（Web Speech API，零成本零延迟），
因此这里不实现 ASR——实测硅基的 SenseVoiceSmall 在服务端太慢（3 秒音频 45s、6 秒音频超时），
不适合通话（详见 ``docs/语音通话方案.md`` 的实测表）。

设计要点：
  - **分句合成**（:func:`split_for_speech`）：长答复不整段合成，第一句合成完就能播；
  - **连接管理**：实测复用失效 keep-alive 连接会出现 20-80s 的假死，因此
    ``keepalive_expiry`` 设短、并对超时/连接错误**重试一次**（用全新连接）；
  - 不可用时给出**可执行的修复指引**（配置项 + 环境变量名），而不是模糊报错；
  - 全部 async（不阻塞事件循环）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx

from app.core.log import logger
from app.voice.audio import split_for_speech

__all__ = ["VoiceError", "VoiceService", "SpeechChunk", "get_voice_service"]


class VoiceError(RuntimeError):
    """语音服务不可用 / 调用失败（带修复指引）。"""


class SpeechChunk:
    """一块待播放的语音。"""

    __slots__ = ("index", "text", "audio", "final")

    def __init__(self, index: int, text: str, audio: bytes, *, final: bool = False) -> None:
        self.index = index
        self.text = text
        self.audio = audio
        self.final = final

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"SpeechChunk(index={self.index}, chars={len(self.text)}, bytes={len(self.audio)}, final={self.final})"


class VoiceService:
    """TTS 封装（无状态，进程内单例复用连接）。"""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ 配置

    def _config(self):
        from app.config import get_app_config

        return get_app_config().voice

    @property
    def available(self) -> bool:
        config = self._config()
        return bool(config.enabled and config.api_key)

    def unavailable_reason(self) -> str:
        """不可用原因（含修复指引）。"""
        config = self._config()
        if not config.enabled:
            return "语音通话未启用（config.yaml voice.enabled=false）"
        if not config.api_key:
            return f"缺少语音接口 Key：请在 .env 配置 {config.api_key_env}（对应 config.yaml voice.api_key_env）"
        return ""

    def _ensure_available(self) -> None:
        reason = self.unavailable_reason()
        if reason:
            raise VoiceError(reason)

    async def _http(self) -> httpx.AsyncClient:
        """共享客户端：短 keep-alive（避免拿到失效连接）+ 有限连接池。"""
        if self._client is None:
            config = self._config()
            self._client = httpx.AsyncClient(
                base_url=config.base_url.rstrip("/"),
                headers={"Authorization": f"Bearer {config.api_key}"},
                timeout=httpx.Timeout(config.timeout, connect=15.0),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4, keepalive_expiry=5.0),
            )
        return self._client

    async def aclose(self) -> None:
        """释放连接（应用 shutdown 调用）。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ 合成

    async def synthesize(self, text: str) -> bytes:
        """合成单段文本为 MP3；空文本返回空字节。

        失败时重试一次（换全新连接）：实测复用失效连接会长时间假死，重试能显著降低"卡住"概率。
        """
        speech = (text or "").strip()
        if not speech:
            return b""
        self._ensure_available()
        config = self._config()

        last_error: Exception | None = None
        for attempt in (1, 2):
            try:
                client = await self._http() if attempt == 1 else None
                if client is None:
                    # 第二次尝试：用独立连接，绕开可能已失效的池化连接
                    async with httpx.AsyncClient(
                        base_url=config.base_url.rstrip("/"),
                        headers={"Authorization": f"Bearer {config.api_key}"},
                        timeout=httpx.Timeout(config.timeout, connect=15.0),
                    ) as fresh:
                        return await self._post_speech(fresh, speech)
                return await self._post_speech(client, speech)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                logger.warning("[voice] TTS 第 {} 次失败（{}），重试一次", attempt, type(exc).__name__)
            except VoiceError:
                raise
        raise VoiceError(f"语音合成失败（重试后仍不可用）: {last_error}")

    async def _post_speech(self, client: httpx.AsyncClient, speech: str) -> bytes:
        """真正发一次 TTS 请求。"""
        config = self._config()
        response = await client.post(
            "/audio/speech",
            json={"model": config.tts_model, "voice": config.tts_voice, "input": speech, "response_format": "mp3"},
        )
        if response.status_code != 200:
            raise VoiceError(f"语音合成失败（HTTP {response.status_code}）: {response.text[:200]}")
        logger.debug("[voice] TTS: {} 字 -> {} 字节", len(speech), len(response.content))
        return response.content

    async def synthesize_chunks(self, text: str) -> AsyncIterator[SpeechChunk]:
        """**按句流式**合成：切块后逐块合成并 yield，调用方收到一块就能立刻发给前端播放。

        单块失败不中断整段播报（错误记日志并跳过该块），最后一块带 ``final=True``。
        """
        chunks = split_for_speech(
            text,
            max_chars=self._config().chunk_max_chars,
            min_chars=self._config().chunk_min_chars,
            first_max_chars=self._config().first_chunk_max_chars,
            max_chunks=self._config().max_chunks,
        )
        if not chunks:
            return
        for index, chunk in enumerate(chunks):
            try:
                audio = await self.synthesize(chunk)
            except VoiceError as exc:
                logger.warning("[voice] 第 {} 块合成失败，跳过: {}", index + 1, exc)
                audio = b""
            yield SpeechChunk(index, chunk, audio, final=index == len(chunks) - 1)
            # 让出事件循环：合成是网络等待，避免连续块把心跳/取消信号压住
            await asyncio.sleep(0)


_service: VoiceService | None = None


def get_voice_service() -> VoiceService:
    """全局语音服务单例。"""
    global _service
    if _service is None:
        _service = VoiceService()
    return _service
