<script setup>
/**
 * 通话面板：页内与 Agent 语音对话（波浪动效 + 实时字幕 + 计划卡片 + 打断）。
 *
 * 数据流（见 docs/语音通话方案.md）：
 *   麦克风 → 浏览器识别（边说边出字）→ 整句定稿 emit('utterance') → 父组件走 /chat 或 /resume（voice）
 *   → 服务端按句合成 → 父组件 enqueueAudio() 交给本组件 → 顺序播放并显示字幕
 *
 * 四种状态的视觉与文案（对齐"豆包/千问"那种有呼吸感的通话界面）：
 *   listening        麦克风电平驱动的蓝色波形 + 实时字幕"我在听"
 *   thinking         紫色呼吸/旋转，明确告知"正在思考，请稍等"+ 已等待秒数，可【打断】
 *   speaking         波形按语音韵律起伏 + 当前朗读句高亮字幕，可【打断】
 *   awaiting_confirm 展示计划步骤卡片，可【继续执行】或【提交意见】（也可直接说"继续"）
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { createLevelMeter, createRecognizer, fallbackSpeak, fallbackStop, SpeechQueue, speechRecognitionSupported } from '../utils/voice'

const props = defineProps({
  /** 通话是否进行中（由父组件控制挂载） */
  active: { type: Boolean, default: true },
  /** 状态：listening | thinking | speaking | awaiting_confirm */
  state: { type: String, default: 'listening' },
  /** 等待确认的计划载荷（interrupt payload：{title, items, questions}） */
  interruptPayload: { type: Object, default: null },
  /** 会话标题（显示"正在与…通话"） */
  title: { type: String, default: '' },
})

const emit = defineEmits(['utterance', 'hangup', 'confirm', 'interrupt', 'state'])

const supported = speechRecognitionSupported()
const muted = ref(false)
const interim = ref('')
const lastHeard = ref('')
const errorMsg = ref('')
const seconds = ref(0)
const thinkingSeconds = ref(0)
const subtitle = ref('')
const spokenHistory = ref([])
const waveform = ref(null)

let recognizer = null
let timer = null
let thinkTimer = null
let rafId = null
const queue = new SpeechQueue()
const meter = createLevelMeter()

/** 当前状态是否为"AI 在忙"（此时提供打断） */
const busy = computed(() => props.state === 'thinking' || props.state === 'speaking')

const stateText = computed(() => {
  if (errorMsg.value) return errorMsg.value
  if (props.state === 'awaiting_confirm') return '计划已生成，确认后开始执行'
  if (props.state === 'thinking') return `正在思考，请稍等（已等待 ${thinkingSeconds.value}s，可打断）`
  if (props.state === 'speaking') return '正在回答（可打断）'
  return muted.value ? '已静音（点麦克风恢复）' : '我在听，请说'
})

const timerText = computed(() => {
  const m = String(Math.floor(seconds.value / 60)).padStart(2, '0')
  const s = String(seconds.value % 60).padStart(2, '0')
  return `${m}:${s}`
})

const planItems = computed(() => props.interruptPayload?.items || [])
const planTitle = computed(() => props.interruptPayload?.title || '计划已生成')

/** 播放一块服务端合成音频（字幕用事件里带的文本，逐句高亮） */
function enqueueAudio(base64, format = 'mp3', text = '') {
  errorMsg.value = ''
  if (text) {
    subtitle.value = text
    spokenHistory.value = [...spokenHistory.value.slice(-3), text]
  }
  queue.push(base64, format, text)
}

/** 服务端 TTS 不可用：用浏览器内置朗读兜底（同时把文字放上字幕） */
function speakFallback(text) {
  subtitle.value = text
  fallbackSpeak(text, () => queue.onStateChange?.('listening'))
}

function startRecognizer() {
  if (!supported || recognizer) return
  recognizer = createRecognizer({
    onResult(text, isFinal) {
      if (isFinal) {
        interim.value = ''
        lastHeard.value = text
        emit('utterance', text)
      } else {
        interim.value = text
      }
    },
    onError(msg) {
      errorMsg.value = msg
    },
  })
  recognizer?.start()
}

function stopRecognizer() {
  recognizer?.abort()
  recognizer = null
  interim.value = ''
}

function toggleMute() {
  muted.value = !muted.value
  if (muted.value) recognizer?.stop()
  else recognizer?.start()
}

function hangup() {
  stopRecognizer()
  queue.stop()
  fallbackStop()
  meter.stop()
  cancelAnimationFrame(rafId)
  emit('hangup')
}

/** 打断：停止播报/朗读并交给父组件中止这一轮（之后可以继续说） */
function interrupt() {
  queue.stop()
  fallbackStop()
  subtitle.value = ''
  emit('interrupt')
}

// ------------------------------------------------------------------ 波形动效

const BAR_COUNT = 44

function drawWave() {
  rafId = requestAnimationFrame(drawWave)
  const canvas = waveform.value
  if (!canvas) return
  const dpr = window.devicePixelRatio || 1
  const width = canvas.clientWidth
  const height = canvas.clientHeight
  if (canvas.width !== width * dpr || canvas.height !== height * dpr) {
    canvas.width = width * dpr
    canvas.height = height * dpr
  }
  const ctx = canvas.getContext('2d')
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, width, height)

  const t = performance.now() / 1000
  const mic = props.state === 'listening' ? meter.level() : 0
  const palette =
    props.state === 'thinking'
      ? ['#a78bfa', '#8b5cf6']
      : props.state === 'speaking'
        ? ['#34d399', '#06b6d4']
        : ['#60a5fa', '#6366f1']

  const gradient = ctx.createLinearGradient(0, 0, width, 0)
  gradient.addColorStop(0, palette[0])
  gradient.addColorStop(1, palette[1])
  ctx.fillStyle = gradient

  const barWidth = Math.max(2, width / (BAR_COUNT * 1.8))
  const gap = (width - barWidth * BAR_COUNT) / (BAR_COUNT - 1)

  for (let i = 0; i < BAR_COUNT; i++) {
    const norm = i / (BAR_COUNT - 1)
    let amplitude
    if (props.state === 'listening') {
      // 聆听：麦克风电平 × 中间高两边低的包络，安静时留一条细线
      const envelope = 0.35 + 0.65 * Math.sin(Math.PI * norm)
      amplitude = 0.06 + mic * envelope
    } else if (props.state === 'thinking') {
      // 思考：缓慢流动的呼吸波，告诉用户"在忙，不是卡住"
      amplitude = 0.18 + 0.22 * (0.5 + 0.5 * Math.sin(t * 2.2 + norm * 6))
    } else if (props.state === 'speaking') {
      // 说话：几个不同频率叠加，接近语音韵律
      amplitude =
        0.25 +
        0.35 * Math.abs(Math.sin(t * 6 + norm * 9)) * (0.6 + 0.4 * Math.sin(t * 3.1 + norm * 4)) +
        0.12 * Math.abs(Math.sin(t * 11 + norm * 17))
    } else {
      amplitude = 0.16 + 0.1 * Math.sin(t * 1.6 + norm * 5)
    }
    amplitude = Math.max(0.05, Math.min(1, amplitude))
    const barHeight = amplitude * height * 0.9
    const x = i * (barWidth + gap)
    const y = (height - barHeight) / 2
    ctx.beginPath()
    const r = barWidth / 2
    ctx.roundRect?.(x, y, barWidth, barHeight, r)
    if (!ctx.roundRect) ctx.rect(x, y, barWidth, barHeight)
    ctx.fill()
  }
}

watch(
  () => props.state,
  (state) => {
    if (state === 'thinking') {
      thinkingSeconds.value = 0
      thinkTimer = setInterval(() => (thinkingSeconds.value += 1), 1000)
    } else if (thinkTimer) {
      clearInterval(thinkTimer)
      thinkTimer = null
    }
  }
)

watch(
  () => props.active,
  async (active) => {
    if (active) {
      queue.unlock() // 用户点击手势里解锁播放
      queue.onStateChange = (s) => {
        // 'speaking' 表示真的在播报（父组件据此把面板从"思考中"切到"正在回答"）
        if (s === 'listening') subtitle.value = ''
        emit('state', s)
      }
      startRecognizer()
      meter.start()
      cancelAnimationFrame(rafId)
      drawWave()
      seconds.value = 0
      timer = setInterval(() => (seconds.value += 1), 1000)
    } else {
      stopRecognizer()
      queue.stop()
      fallbackStop()
      meter.stop()
      cancelAnimationFrame(rafId)
      if (timer) clearInterval(timer)
      if (thinkTimer) clearInterval(thinkTimer)
      timer = null
      thinkTimer = null
    }
  },
  { immediate: true }
)

onBeforeUnmount(() => {
  stopRecognizer()
  queue.stop()
  fallbackStop()
  meter.stop()
  cancelAnimationFrame(rafId)
  if (timer) clearInterval(timer)
  if (thinkTimer) clearInterval(thinkTimer)
})

defineExpose({ enqueueAudio, speakFallback, stopAudio: () => queue.stop(), isBusy: () => queue.busy })
</script>

<template>
  <div class="call-overlay">
    <div class="call-card" :class="`state-${state}`">
      <div class="top">
        <span class="peer">{{ title || '与 Agent 通话中' }}</span>
        <span class="timer">{{ timerText }}</span>
      </div>

      <div class="viz">
        <div class="avatar" :class="state">
          <span class="ring" />
          <span class="ring delay" />
          <span class="glyph">{{ state === 'thinking' ? '· · ·' : '🦌' }}</span>
        </div>
        <canvas ref="waveform" class="wave" />
      </div>

      <div class="status" :class="{ warn: Boolean(errorMsg) }">{{ stateText }}</div>

      <div class="subtitle">
        <template v-if="state === 'speaking' && subtitle">“{{ subtitle }}”</template>
        <template v-else-if="interim">“{{ interim }}”</template>
        <template v-else-if="lastHeard">你刚才说：{{ lastHeard }}</template>
        <template v-else-if="!supported">当前浏览器不支持语音识别，请改用 Chrome/Edge 或直接打字</template>
        <template v-else-if="state === 'awaiting_confirm'">说「继续」开始执行，或直接说你的意见</template>
        <template v-else>说出你的问题，停顿一下我就开始处理</template>
      </div>

      <div v-if="state === 'awaiting_confirm' && planItems.length" class="plan">
        <div class="plan-title">{{ planTitle }}</div>
        <ol class="plan-list">
          <li v-for="(it, i) in planItems" :key="it.plan_id || i">
            <span class="step-name">{{ it.name || it.plan_id }}</span>
            <span v-if="it.desc" class="step-desc">{{ it.desc }}</span>
          </li>
        </ol>
      </div>

      <div class="actions">
        <button class="round" :class="{ off: muted }" :title="muted ? '取消静音' : '静音'" @click="toggleMute">
          {{ muted ? '🔇' : '🎙️' }}
        </button>

        <button v-if="state === 'awaiting_confirm'" class="wide primary" @click="emit('confirm')">继续执行</button>
        <button v-else-if="busy" class="wide" @click="interrupt">打断</button>
        <span v-else class="spacer" />

        <button class="round hangup" title="挂断" @click="hangup">📞</button>
      </div>

      <div class="hint">识别在本机浏览器完成；回答会同时显示在对话里</div>
    </div>
  </div>
</template>

<style scoped>
.call-overlay {
  position: absolute;
  inset: 0;
  z-index: 60;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(15, 23, 42, 0.6);
  backdrop-filter: blur(3px);
}

.call-card {
  width: min(94%, 400px);
  padding: 18px 20px 14px;
  border-radius: 20px;
  background: var(--panel);
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.3);
  text-align: center;
  transition: box-shadow 0.3s ease;
}

.call-card.state-thinking {
  box-shadow: 0 24px 60px rgba(139, 92, 246, 0.28);
}

.call-card.state-speaking {
  box-shadow: 0 24px 60px rgba(16, 185, 129, 0.25);
}

.top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  color: var(--text-2);
}

.peer {
  max-width: 70%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.timer {
  font-variant-numeric: tabular-nums;
}

.viz {
  position: relative;
  height: 132px;
  margin: 6px 0 4px;
}

.avatar {
  position: absolute;
  left: 50%;
  top: 50%;
  width: 76px;
  height: 76px;
  margin: -38px 0 0 -38px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 30px;
  background: linear-gradient(135deg, #eef2ff, #e0f2fe);
  z-index: 2;
}

.avatar .glyph {
  position: relative;
  z-index: 2;
}

.avatar.thinking .glyph {
  font-size: 20px;
  letter-spacing: 2px;
  color: #8b5cf6;
  animation: blink 1.1s ease-in-out infinite;
}

@keyframes blink {
  0%, 100% { opacity: 0.35; }
  50% { opacity: 1; }
}

.ring {
  position: absolute;
  inset: 0;
  border-radius: 50%;
  border: 2px solid rgba(99, 102, 241, 0.35);
  animation: ripple 2.4s ease-out infinite;
}

.ring.delay {
  animation-delay: 1.2s;
}

.state-thinking .ring {
  border-color: rgba(139, 92, 246, 0.45);
}

.state-speaking .ring {
  border-color: rgba(16, 185, 129, 0.45);
}

@keyframes ripple {
  0% { transform: scale(1); opacity: 0.7; }
  100% { transform: scale(1.75); opacity: 0; }
}

.wave {
  width: 100%;
  height: 100%;
  position: relative;
  z-index: 1;
}

.status {
  font-size: 14px;
  color: var(--accent);
  min-height: 20px;
  margin-bottom: 6px;
}

.state-thinking .status { color: #7c3aed; }
.state-speaking .status { color: #059669; }
.status.warn { color: #c03034; }

.subtitle {
  min-height: 42px;
  font-size: 13px;
  line-height: 1.65;
  color: var(--text-2);
  padding: 0 4px 8px;
  word-break: break-word;
}

.plan {
  text-align: left;
  margin: 0 0 10px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: #fafbff;
  max-height: 170px;
  overflow-y: auto;
}

.plan-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 6px;
}

.plan-list {
  margin: 0;
  padding-left: 18px;
}

.plan-list li {
  font-size: 13px;
  line-height: 1.6;
}

.step-name { font-weight: 600; }

.step-desc {
  display: block;
  font-size: 12px;
  color: var(--text-2);
}

.actions {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  margin: 4px 0 10px;
}

.round {
  width: 50px;
  height: 50px;
  border-radius: 50%;
  font-size: 20px;
  background: #f1f3f7;
  flex-shrink: 0;
}

.round.off { background: #ffe9e9; }

.round.hangup {
  background: #e5484d;
  color: #fff;
}

.wide {
  padding: 10px 18px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: #fff;
  font-size: 14px;
  color: var(--text);
}

.wide.primary {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
  font-weight: 600;
}

.spacer { width: 76px; }

.hint {
  font-size: 11px;
  color: var(--text-2);
  opacity: 0.85;
}
</style>
