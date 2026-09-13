/**
 * 语音通话前端支撑：浏览器原生语音识别（STT）+ 语音排队播放（TTS 音频块）。
 *
 * 设计取舍（见 docs/语音通话方案.md）：
 *   - 识别用 Web Speech API：零成本、边说边出字（部分结果）、无需上传音频；
 *     Chrome/Edge 支持，Safari 较新版本可用，Firefox 不支持（此时界面会提示改用文字）。
 *   - 合成在服务端（硅基 CosyVoice2，按句流式），前端只负责**排队播放**收到的 mp3 块，
 *     这样长答复不用等整段合成完。
 *   - 服务端合成失败时，回退到浏览器内置 speechSynthesis 朗读，保证通话不静默。
 */

/** 语音识别是否可用（浏览器支持情况） */
export function speechRecognitionSupported() {
  return typeof window !== 'undefined' && Boolean(window.SpeechRecognition || window.webkitSpeechRecognition)
}

/**
 * 创建一次语音识别会话（返回 controller）。
 *
 * @param {Object} opts
 * @param {(text: string, isFinal: boolean) => void} opts.onResult
 * @param {(msg: string) => void} [opts.onError]
 * @param {string} [opts.lang]
 */
export function createRecognizer({ onResult, onError, lang = 'zh-CN' } = {}) {
  const Impl = window.SpeechRecognition || window.webkitSpeechRecognition
  if (!Impl) return null

  const recognition = new Impl()
  recognition.lang = lang
  recognition.continuous = true
  recognition.interimResults = true

  recognition.onresult = (event) => {
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const result = event.results[i]
      const text = (result[0]?.transcript || '').trim()
      if (text) onResult?.(text, result.isFinal)
    }
  }
  recognition.onerror = (event) => {
    // no-speech / aborted 是正常情况，不当作错误提示
    if (event?.error && !['no-speech', 'aborted'].includes(event.error)) {
      onError?.(`语音识别出错：${event.error}`)
    }
  }

  let wantRunning = false
  recognition.onend = () => {
    // 浏览器会在静默后自动结束，通话中需要自动重启
    if (wantRunning) {
      try {
        recognition.start()
      } catch {
        /* 已经在运行时忽略 */
      }
    }
  }

  return {
    start() {
      wantRunning = true
      try {
        recognition.start()
      } catch {
        /* ignore */
      }
    },
    stop() {
      wantRunning = false
      try {
        recognition.stop()
      } catch {
        /* ignore */
      }
    },
    abort() {
      wantRunning = false
      try {
        recognition.abort()
      } catch {
        /* ignore */
      }
    },
  }
}

/**
 * 麦克风电平表：用 AnalyserNode 实时算音量（0..1），驱动"正在聆听"的波形动效。
 * 拿不到麦克风（未授权/不支持）时静默降级为 0，不影响通话与识别。
 */
export function createLevelMeter() {
  let ctx = null
  let analyser = null
  let stream = null
  let buffer = null

  async function start() {
    if (analyser) return true
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const Ctx = window.AudioContext || window.webkitAudioContext
      ctx = new Ctx()
      const source = ctx.createMediaStreamSource(stream)
      analyser = ctx.createAnalyser()
      analyser.fftSize = 512
      analyser.smoothingTimeConstant = 0.75
      source.connect(analyser)
      buffer = new Uint8Array(analyser.frequencyBinCount)
      return true
    } catch {
      stream = null
      analyser = null
      return false
    }
  }

  /** 当前音量（0..1，已做平滑） */
  function level() {
    if (!analyser) return 0
    analyser.getByteTimeDomainData(buffer)
    let sum = 0
    for (let i = 0; i < buffer.length; i++) {
      const v = (buffer[i] - 128) / 128
      sum += v * v
    }
    const rms = Math.sqrt(sum / buffer.length)
    return Math.min(1, rms * 4) // 说话时 RMS 偏小，放大到视觉可用
  }

  function stop() {
    try {
      stream?.getTracks().forEach((t) => t.stop())
      ctx?.close()
    } catch {
      /* ignore */
    }
    ctx = null
    analyser = null
    stream = null
  }

  return { start, level, stop }
}

/**
 * 语音播放队列：按顺序播放收到的音频块，避免句与句重叠或抢麦。
 *
 * 通过 ``onChunkStart`` 回调把"正在朗读的句子"交给界面做字幕（用户能看到 AI 在说什么）。
 */
export class SpeechQueue {
  constructor() {
    this.queue = []
    this.playing = false
    this.current = null
    this.onStateChange = null
    this.onChunkStart = null
    this.onChunkEnd = null
    this.unlocked = false
  }

  /**
   * 解锁音频播放（浏览器自动播放策略：必须在用户手势的调用栈里"播过一次"）。
   * 播一段静音 WAV，之后程序化播放合成的语音才不会被拦截。
   */
  unlock() {
    if (this.unlocked) return
    try {
      // 44 字节的极短静音 WAV
      const silent = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAgD4AAAB9AAACABAAZGF0YQAAAAA='
      const audio = new Audio(silent)
      audio.volume = 0
      audio.play().then(() => {
        this.unlocked = true
      }).catch(() => {
        /* 忽略：真正播放时还会再尝试一次 */
      })
    } catch {
      /* ignore */
    }
  }

  /** 入队一块 base64 mp3（带上对应文本，用于字幕） */
  push(base64, format = 'mp3', text = '') {
    if (!base64) return
    this.queue.push({ base64, format, text })
    this._next()
  }

  _next() {
    if (this.playing || !this.queue.length) return
    const item = this.queue.shift()
    this.playing = true
    this.onStateChange?.('speaking')
    this.onChunkStart?.(item.text || '')

    try {
      const binary = atob(item.base64)
      const bytes = new Uint8Array(binary.length)
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
      const url = URL.createObjectURL(new Blob([bytes], { type: `audio/${item.format}` }))
      const audio = new Audio(url)
      this.current = { audio, url }
      const done = () => {
        URL.revokeObjectURL(url)
        this.current = null
        this.playing = false
        this.onChunkEnd?.()
        if (!this.queue.length) this.onStateChange?.('listening')
        this._next()
      }
      audio.onended = done
      audio.onerror = (event) => {
        console.warn('[voice] 音频块播放失败', event)
        done()
      }
      audio.play().catch((err) => {
        console.warn('[voice] 播放被拦截或失败（通常是缺少用户手势）', err)
        done()
      })
    } catch {
      this.playing = false
      this._next()
    }
  }

  /** 停止播放并清空队列（挂断 / 用户打断） */
  stop() {
    this.queue = []
    if (this.current) {
      try {
        this.current.audio.pause()
        URL.revokeObjectURL(this.current.url)
      } catch {
        /* ignore */
      }
      this.current = null
    }
    this.playing = false
  }

  get busy() {
    return this.playing || this.queue.length > 0
  }
}

/**
 * 浏览器内置朗读（服务端 TTS 不可用时的兜底，免费即时）。
 * @param {string} text
 * @param {() => void} [onDone]
 */
export function fallbackSpeak(text, onDone) {
  try {
    const synth = window.speechSynthesis
    if (!synth) {
      onDone?.()
      return
    }
    synth.cancel()
    const utterance = new SpeechSynthesisUtterance(String(text || '').slice(0, 500))
    utterance.lang = 'zh-CN'
    utterance.onend = () => onDone?.()
    utterance.onerror = () => onDone?.()
    const zh = synth.getVoices().find((v) => v.lang?.toLowerCase().startsWith('zh'))
    if (zh) utterance.voice = zh
    synth.speak(utterance)
  } catch {
    onDone?.()
  }
}

/** 停止浏览器内置朗读 */
export function fallbackStop() {
  try {
    window.speechSynthesis?.cancel()
  } catch {
    /* ignore */
  }
}

//: 视为"照此执行"的口语（语音确认场景）
const CONTINUE_WORDS = ['继续', '可以', '好的', '好', '行', '开始', '执行', '确认', '没问题', 'ok', 'okay', 'yes', 'go']

/**
 * 用户这句话是不是"照此执行"（语音确认计划时用）。
 * 长句更可能是意见，直接判为意见。
 *
 * @param {string} text
 */
export function isContinueIntent(text) {
  const normalized = String(text || '').replace(/[\s，。,.!！?？]/g, '').toLowerCase()
  if (!normalized) return true
  if (normalized.length > 12) return false
  return CONTINUE_WORDS.some((word) => normalized.includes(word))
}
