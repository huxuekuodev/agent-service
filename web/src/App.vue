<script setup>
import { ref, onMounted, nextTick } from 'vue'
import Sidebar from './components/Sidebar.vue'
import ChatArea from './components/ChatArea.vue'
import MonitorView from './components/MonitorView.vue'
import LoginView from './components/LoginView.vue'
import ConfirmCard from './components/ConfirmCard.vue'
import CallPanel from './components/CallPanel.vue'
import { isContinueIntent } from './utils/voice'
import {
  authMe,
  authLogout,
  createSession,
  getMessages,
  getSession,
  hasToken,
  getStoredUser,
  clearAuth,
  listSessions,
  deleteSession,
  chatStream,
  resumeSession,
} from './api'

const sessions = ref([])
const currentId = ref(null)
const messages = ref([])
const thinking = ref('')
const progress = ref([]) // 执行过程（步骤/工具调用）
const streaming = ref(false)
const sidebarOpen = ref(false)
const error = ref('')
const view = ref('chat') // chat | monitor
const monitorRef = ref(null)

// 登录态：booting（启动校验中）→ user 非空即已登录
const user = ref(getStoredUser())
const booting = ref(true)

// 历史分页：更早消息的游标
const olderSeq = ref(null)
const hasOlder = ref(false)
const loadingOlder = ref(false)

const CURRENT_SESSION_KEY = 'dsh.currentSessionId'

const THINK = 'thinkMessage'

// 事件类型（与后端 app/agents/events.py 的 EventType 对齐）
const EV = {
  THINK: 'thinkMessage',
  THINKING: 'thinking',
  TOOL_CALL: 'tool_call',
  TOOL_RESULT: 'tool_result',
  PLAN: 'plan',
  STEP: 'step',
  CLARIFY: 'clarify',
  ANSWER: 'answer',
  ERROR: 'error',
  INTERRUPT: 'interrupt',
}

// 等待用户确认的中断（plan_review 等）：非空时展示确认卡片，输入框交互改走 /resume
const interrupt = ref(null)
const confirmBusy = ref(false)
let pendingResumeText = null

// 语音通话：callState 驱动面板状态，voiceRef 用于把服务端合成的音频交给面板播放
const inCall = ref(false)
const callState = ref('listening')
const voiceRef = ref(null)
const fallbackSpeech = ref('')
let currentAbort = null
//: 本轮待重试的语音（遇到 1103：会话其实在等确认，应改走 /resume 而不是丢弃用户这句话）

// 事件 → 进度行（执行过程可视化）
function progressLine(ev) {
  switch (ev.type) {
    case EV.STEP:
      return `${ev.status === 'started' ? '▶️' : ev.status === 'completed' ? '✅' : '❌'} ${ev.name || ev.plan_id}${ev.detail ? '：' + String(ev.detail).slice(0, 80) : ''}`
    case EV.TOOL_CALL:
      return `🔧 调用 ${ev.name}`
    case EV.TOOL_RESULT:
      return `${ev.ok === false ? '⚠️' : '📄'} ${ev.name} 返回`
    case EV.PLAN:
      return `📋 计划（${ev.action}）：${ev.task_count || 0} 个任务`
    case EV.CLARIFY:
      return `❓ ${String(ev.content || '').slice(0, 80)}`
    case EV.ERROR:
      return `❌ ${ev.messages || '执行出错'}`
    default:
      return ''
  }
}

// 会话标题：优先服务端 title，其次首条用户消息
function titleOf(session) {
  return (session?.title || '').slice(0, 20) || '新会话'
}

/** 后端会话行 → 侧边栏条目（字段名归一，避免组件里到处判空） */
function toSessionItem(row) {
  return {
    id: row.session_id,
    title: row.title || '',
    preview: row.last_preview || '',
    count: row.message_count || 0,
    updatedAt: row.last_message_at || row.updated_at || row.created_at || '',
  }
}

async function refreshSessions() {
  try {
    const data = await listSessions({ limit: 50 })
    sessions.value = (data?.sessions || []).map(toSessionItem)
  } catch (e) {
    if (handleAuthError(e)) return
    console.warn('刷新会话列表失败:', e)
  }
}

/** 认证类错误统一处理：清登录态并回到登录页 */
function handleAuthError(e) {
  const status = e?.status
  if (status === 1200 || status === 1201 || status === 1202) {
    clearAuth()
    user.value = null
    error.value = '登录已失效，请重新登录'
    return true
  }
  return false
}

async function handleCreate() {
  try {
    const data = await createSession()
    const session = toSessionItem(data)
    sessions.value.unshift(session)
    await switchSession(session.id)
    sidebarOpen.value = false
  } catch (e) {
    if (handleAuthError(e)) return
    error.value = e.message || '创建会话失败'
  }
}

/** 切换会话：清空当前视图并拉取该会话历史 */
async function switchSession(id) {
  currentId.value = id
  messages.value = []
  thinking.value = ''
  progress.value = []
  error.value = ''
  olderSeq.value = null
  hasOlder.value = false
  try {
    localStorage.setItem(CURRENT_SESSION_KEY, id)
  } catch {
    /* ignore */
  }
  await loadHistory(id)
  await syncPendingInterrupt(id)
}

/** 拉取会话详情里的挂起中断（刷新/切会话后恢复确认卡片） */
async function syncPendingInterrupt(id) {
  interrupt.value = null
  try {
    const detail = await getSession(id)
    if (detail?.pending_interrupt) {
      interrupt.value = {
        interruptId: detail.pending_interrupt.interrupt_id,
        payload: detail.pending_interrupt.payload || {},
      }
    }
  } catch (e) {
    if (!handleAuthError(e)) console.warn('读取待确认状态失败:', e)
  }
}

/** 拉取历史消息（倒序取页、正序返回） */
async function loadHistory(id, beforeSeq = null) {
  try {
    const data = await getMessages(id, { limit: 50, before_seq: beforeSeq })
    const rows = data?.messages || []
    const bubbles = rows.map((m) => ({
      role: m.role === 'user' ? 'user' : 'assistant',
      content: m.content,
      kind: m.kind,
      avatar: m.role === 'user' ? undefined : '🦌',
      createdAt: m.created_at,
    }))
    messages.value = beforeSeq ? [...bubbles, ...messages.value] : bubbles
    olderSeq.value = data?.before_seq ?? null
    hasOlder.value = Boolean(data?.has_more)
  } catch (e) {
    if (handleAuthError(e)) return
    error.value = e.message || '加载历史失败'
  }
}

async function loadOlder() {
  if (!currentId.value || !olderSeq.value || loadingOlder.value) return
  loadingOlder.value = true
  try {
    await loadHistory(currentId.value, olderSeq.value)
  } finally {
    loadingOlder.value = false
  }
}

async function handleDelete(session) {
  const confirmed = window.confirm(`确定删除会话「${titleOf(session)}」吗？`)
  if (!confirmed) return
  try {
    await deleteSession(session.id)
    sessions.value = sessions.value.filter((s) => s.id !== session.id)
    if (currentId.value === session.id) {
      currentId.value = null
      messages.value = []
      try {
        localStorage.removeItem(CURRENT_SESSION_KEY)
      } catch {
        /* ignore */
      }
    }
  } catch (e) {
    if (handleAuthError(e)) return
    error.value = e.message || '删除会话失败'
  }
}

async function handleLogout() {
  await authLogout(false)
  user.value = null
  sessions.value = []
  messages.value = []
  currentId.value = null
  try {
    localStorage.removeItem(CURRENT_SESSION_KEY)
  } catch {
    /* ignore */
  }
}

function onLoginSuccess(profile) {
  user.value = profile
  error.value = ''
  bootstrapWorkspace()
}

/** 登录后加载工作区：会话列表 + 恢复上次会话 */
async function bootstrapWorkspace() {
  await refreshSessions()
  let saved = ''
  try {
    saved = localStorage.getItem(CURRENT_SESSION_KEY) || ''
  } catch {
    saved = ''
  }
  const target = sessions.value.find((s) => s.id === saved) || sessions.value[0]
  if (target) await switchSession(target.id)
  else currentId.value = null
}

/** 生成消息幂等 id（重发同一 id 不会产生重复消息） */
function newClientMsgId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  return `c-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

/** 处理一条后端 SSE 事件（chat 与 resume 共用） */
/**
 * 处理一条后端 SSE 事件（chat 与 resume 共用）。
 *
 * 载荷形状有两类，混用会静默失效：
 *   - `custom`：业务事件**嵌套**在 event.data 里（thinkMessage/plan/step/answer/…）；
 *   - `interrupt`：嵌套，event.data = {interrupt_id, payload}；
 *   - `voice_audio` / `voice_unavailable` / `voice_end` / `end`：**扁平**，字段就在 event 上。
 */
function applyStreamEvent(event, ctx) {
  const type = event.type
  const aiIndex = ctx.aiIndex

  if (type === 'custom') {
    // 业务事件嵌套在 event.data 里（见后端 app/agents/events.py）
    const inner = event.data || {}
    if (inner.type === EV.THINK) {
      thinking.value = inner.messages || inner.content || '思考中…'
      return
    }
    if (inner.type === EV.THINKING) {
      // 模型流式增量：追加到思考条（打字机）
      thinking.value = (thinking.value || '') + (inner.delta || '')
      return
    }
    if (inner.type === EV.ANSWER || inner.type === EV.CLARIFY) {
      // 事件驱动的最终答复 / 澄清问题：直接落到当前助手气泡
      const text = inner.content || ''
      if (text && messages.value[aiIndex]) {
        messages.value[aiIndex].content = messages.value[aiIndex].content || text
      }
      thinking.value = ''
      return
    }
    const line = progressLine(inner)
    if (line) progress.value.push(line)
    return
  }

  if (type === 'messages') {
    // 增量 token：data 为 [msgChunk, metadata]，取 msgChunk.content 追加
    const parts = event.data
    const list = Array.isArray(parts) ? parts : [parts]
    for (const item of list) {
      const content = typeof item === 'string' ? item : (item?.content ?? '')
      if (typeof content === 'string' && content.trim() && messages.value[aiIndex]) {
        messages.value[aiIndex].content += content
      }
    }
    return
  }

  if (type === 'values') {
    // 完整状态快照：最终答案由「新增的 AIMessage」承载，跳过 ToolMessage 等内部消息
    const data = event.data || {}
    const msgList = data.messages
    if (Array.isArray(msgList) && !messages.value[aiIndex]?.content) {
      for (let i = msgList.length - 1; i >= 0; i--) {
        const m = msgList[i]
        if (!m || typeof m.type !== 'string') continue
        if (m.type === 'HumanMessage') break
        if (m.type !== 'AIMessage') continue
        const c = m.content
        if (typeof c === 'string' && c.trim()) {
          messages.value[aiIndex].content = c
          break
        }
      }
    }
    return
  }

  if (type === EV.INTERRUPT) {
    // 运行被挂起：展示确认卡片，等用户答复后走 /resume
    interrupt.value = {
      interruptId: event.data?.interrupt_id || '',
      payload: event.data?.payload || {},
    }
    ctx.interrupted = true
    thinking.value = ''
    return
  }

  if (type === 'voice_audio') {
    // 服务端按句合成的语音块：**载荷是扁平的**（{type,seq,text,format,audio,final}），
    // 不要照 custom 事件那样读 event.data（那是嵌套结构）——读错会静默没声音。
    const audio = event.audio || ''
    const text = event.text || ''
    if (audio) {
      voiceRef.value?.enqueueAudio?.(audio, event.format || 'mp3', text)
    } else if (text) {
      // 没带音频（服务端某块合成失败）：用浏览器朗读兜底，保证这句话能被听到
      voiceRef.value?.speakFallback?.(text)
    } else {
      console.warn('[voice] voice_audio 事件缺少音频内容', event)
    }
    return
  }

  if (type === 'voice_unavailable') {
    // 服务端 TTS 不可用：用浏览器内置朗读兜底（免费即时），保证通话不静默
    const spoken = fallbackSpeech.value
    if (spoken) voiceRef.value?.speakFallback?.(spoken)
    return
  }

  if (type === 'voice_end') {
    // 队列里可能还有没播完的块：播完由队列自己切回"聆听中"；这里只处理"没有音频可播"的情况
    if (!voiceRef.value?.isBusy?.()) {
      callState.value = interrupt.value ? 'awaiting_confirm' : 'listening'
    }
    return
  }

  if (type === 'end') {
    ctx.gotEnd = true
  }
}

/**
 * 跑一次流（新一轮对话 / 恢复中断），统一处理事件、错误与收尾。
 *
 * @param {Object} opts
 * @param {'chat'|'resume'} opts.kind
 * @param {string} [opts.text]      新一轮的用户消息
 * @param {Object} [opts.resume]    { answers, interruptId }
 */
async function runStream({ kind, text = '', resume = null, voice = null }) {
  // 是否要语音播报：默认跟随"当前是否在通话中"——这样新增调用路径不会漏带标记
  // （曾经卡片确认那条路径漏了 voice，导致"计划确认有语音、结果没有"）
  const useVoice = voice === null ? inCall.value : Boolean(voice)
  const sessionId = currentId.value
  messages.value.push({ role: 'assistant', content: '', avatar: '🦌' })
  const ctx = { aiIndex: messages.value.length - 1, gotEnd: false, interrupted: false }

  thinking.value = ''
  progress.value = []
  error.value = ''
  streaming.value = true
  if (kind === 'resume') interrupt.value = null
  currentAbort = new AbortController()
  if (useVoice) {
    callState.value = 'thinking'
    fallbackSpeech.value = ''
  }
  await nextTick()

  const handlers = {
    signal: currentAbort.signal,
    onEvent: (event) => {
      // 记录本轮最终答复文本，供浏览器兜底朗读使用
      const inner = event.type === 'custom' ? event.data || {} : {}
      if (inner.type === 'answer' && inner.content) fallbackSpeech.value = inner.content
      applyStreamEvent(event, ctx)
    },
    onError(err) {
      if (handleAuthError(err)) return
      if (err?.status === 1103 && kind === 'chat') {
        // 会话其实在等确认（例如上一轮被打断在确认处）：刷新卡片，并把这句话改成 resume 重发，
        // 免得用户刚说的话白说
        error.value = ''
        pendingResumeText = text
        syncPendingInterrupt(sessionId)
        return
      }
      error.value = err?.message || '对话失败'
      if (messages.value[ctx.aiIndex] && !messages.value[ctx.aiIndex].content) {
        messages.value[ctx.aiIndex].content = error.value
      }
    },
    onFinally() {
      streaming.value = false
      thinking.value = ''
      confirmBusy.value = false
      if (useVoice && callState.value === 'thinking') callState.value = 'listening'
    },
  }

  try {
    if (kind === 'resume') {
      await resumeSession(
        sessionId,
        { answers: resume?.answers || [], interruptId: resume?.interruptId || '', clientMsgId: newClientMsgId(), voice: useVoice },
        handlers
      )
    } else {
      await chatStream(sessionId, text, { ...handlers, clientMsgId: newClientMsgId(), voice: useVoice })
    }
  } catch (e) {
    streaming.value = false
    thinking.value = ''
    confirmBusy.value = false
    if (!handleAuthError(e) && messages.value[ctx.aiIndex] && !messages.value[ctx.aiIndex].content) {
      messages.value[ctx.aiIndex].content = e.message || '对话失败'
    }
  }

  if (!ctx.gotEnd && !ctx.interrupted && messages.value[ctx.aiIndex] && !messages.value[ctx.aiIndex].content) {
    messages.value[ctx.aiIndex].content = '(无回复)'
  }
  currentAbort = null
  if (user.value) await refreshSessions()

  // 1103 重试：拿到挂起中断后，把刚才那句当作确认/意见重新提交
  if (pendingResumeText !== null) {
    const retryText = pendingResumeText
    pendingResumeText = null
    if (interrupt.value) {
      const answers = isContinueIntent(retryText) ? [] : [{ id: 'review', selected: [], custom: retryText }]
      await runStream({ kind: 'resume', resume: { answers, interruptId: interrupt.value.interruptId }, voice: useVoice })
    }
  }
}

/** 打断当前这一轮：停止播报/朗读、中止 SSE，并同步"是否在等确认"（之后可以继续说） */
async function stopStream() {
  voiceRef.value?.stopAudio?.()
  if (currentAbort) {
    try {
      currentAbort.abort()
    } catch {
      /* ignore */
    }
    currentAbort = null
  }
  streaming.value = false
  thinking.value = ''
  callState.value = 'listening'
  // 后端可能已经停在"计划确认"上：同步一下，下一次说话才会走 /resume
  if (currentId.value) await syncPendingInterrupt(currentId.value)
  if (interrupt.value) callState.value = 'awaiting_confirm'
}

async function handleSend(text) {
  if (!currentId.value) {
    // 无会话时先自动创建
    try {
      const data = await createSession()
      sessions.value.unshift(toSessionItem(data))
      currentId.value = data?.session_id
      try {
        localStorage.setItem(CURRENT_SESSION_KEY, currentId.value)
      } catch {
        /* ignore */
      }
    } catch (e) {
      if (handleAuthError(e)) return
      error.value = e.message || '创建会话失败'
      return
    }
  }

  // 追加用户消息，并本地先更新标题（服务端首条消息会自动生成标题，流结束后同步）
  messages.value.push({ role: 'user', content: text })
  const s = sessions.value.find((x) => x.id === currentId.value)
  if (s && !s.title) s.title = text.slice(0, 20)

  await runStream({ kind: 'chat', text })
}

/** 提交确认卡片：留空 = 继续执行；有意见 = 回规划节点重排 */
async function submitConfirm({ answers }) {
  if (confirmBusy.value || !currentId.value) return
  const feedback = (answers?.[0]?.custom || '').trim()
  confirmBusy.value = true
  messages.value.push({ role: 'user', content: feedback || '（继续执行）' })
  await runStream({ kind: 'resume', resume: { answers, interruptId: interrupt.value?.interruptId || '' } })
}

/** 开始通话（无会话时先建一个，保证语音消息有落点） */
async function startCall() {
  if (!currentId.value) await createSessionForVoice()
  if (!currentId.value) return
  inCall.value = true
  callState.value = 'listening'
  error.value = ''
}

async function createSessionForVoice() {
  try {
    const data = await createSession()
    sessions.value.unshift(toSessionItem(data))
    currentId.value = data?.session_id
  } catch (e) {
    if (handleAuthError(e)) return
    error.value = e.message || '创建会话失败'
  }
}

/** 识别到一整句 → 按当前是否等待确认，走 chat 或 resume */
async function handleVoiceUtterance(text) {
  if (!text?.trim() || streaming.value) return
  messages.value.push({ role: 'user', content: text })
  callState.value = 'thinking'
  if (interrupt.value) {
    // 语音确认：说「继续/可以/开始…」= 无意见直接执行；说别的 = 提意见回规划节点重排
    const answers = isContinueIntent(text) ? [] : [{ id: 'review', selected: [], custom: text }]
    await runStream({ kind: 'resume', resume: { answers, interruptId: interrupt.value.interruptId }, voice: true })
  } else {
    await runStream({ kind: 'chat', text, voice: true })
  }
  callState.value = interrupt.value ? 'awaiting_confirm' : 'listening'
}

/** 通话面板汇报播放状态：正在回答 / 回到聆听 */
function onCallState(state) {
  if (interrupt.value) return // 等确认时状态由 interrupt 决定，别被覆盖
  callState.value = state === 'speaking' ? 'speaking' : 'listening'
}

function endCall() {
  inCall.value = false
  voiceRef.value?.stopAudio?.()
  callState.value = 'listening'
}

onMounted(async () => {
  if (!hasToken()) {
    booting.value = false
    return
  }
  try {
    user.value = await authMe()
    await bootstrapWorkspace()
  } catch (e) {
    if (e?.status === 1200 || e?.status === 1201 || e?.status === 1202) {
      clearAuth()
      user.value = null
    } else {
      error.value = e?.message || '初始化失败'
    }
  } finally {
    booting.value = false
  }
})

function switchView(v) {
  view.value = v
  if (v === 'monitor') {
    // 切到监控页时刷新（组件与 token 汇总）
    nextTick(() => monitorRef.value?.refreshAll?.())
  }
}
</script>

<template>
  <!-- 启动校验中 -->
  <div v-if="booting" class="boot">
    <span>加载中…</span>
  </div>

  <!-- 未登录：登录 / 注册 -->
  <LoginView v-else-if="!user" @success="onLoginSuccess" />

  <div v-else class="layout">
    <Sidebar
      v-if="view === 'chat'"
      :sessions="sessions"
      :current-id="currentId"
      :open="sidebarOpen"
      :user="user"
      @create="handleCreate"
      @select="switchSession"
      @delete="handleDelete"
      @logout="handleLogout"
      @close="sidebarOpen = false"
    />

    <!-- 手机端遮罩 -->
    <div v-if="sidebarOpen && view === 'chat'" class="mask" @click="sidebarOpen = false" />

    <main class="main">
      <header class="topbar">
        <button v-if="view === 'chat'" class="menu-btn" aria-label="菜单" @click="sidebarOpen = true">☰</button>
        <div class="tabs">
          <button :class="['tab', { active: view === 'chat' }]" @click="switchView('chat')">💬 对话</button>
          <button :class="['tab', { active: view === 'monitor' }]" @click="switchView('monitor')">📈 监控</button>
        </div>
        <span v-if="view === 'chat'" class="title">{{ titleOf(sessions.find((s) => s.id === currentId)) }}</span>
        <button
          v-if="view === 'chat'"
          class="call-btn"
          :class="{ active: inCall }"
          :title="inCall ? '结束通话' : '语音通话'"
          @click="inCall ? endCall() : startCall()"
        >
          {{ inCall ? '📞 通话中' : '📞 语音通话' }}
        </button>
      </header>

      <MonitorView
        v-if="view === 'monitor'"
        ref="monitorRef"
        :user-id="user?.user_id || ''"
      />

      <template v-else>
        <div v-if="hasOlder" class="older-bar">
          <button class="older-btn" :disabled="loadingOlder" @click="loadOlder">
            {{ loadingOlder ? '加载中…' : '↑ 加载更早的消息' }}
          </button>
        </div>
        <ChatArea
          :messages="messages"
          :thinking="thinking"
          :progress="progress"
          :streaming="streaming"
          @send="handleSend"
        >
          <template #dock>
            <ConfirmCard
              v-if="interrupt && !streaming"
              :payload="interrupt.payload"
              :busy="confirmBusy"
              @submit="submitConfirm"
            />
          </template>
        </ChatArea>
      </template>

      <CallPanel
        v-if="inCall"
        ref="voiceRef"
        :active="inCall"
        :state="interrupt ? 'awaiting_confirm' : callState"
        :interrupt-payload="interrupt?.payload || null"
        :title="titleOf(sessions.find((s) => s.id === currentId))"
        @utterance="handleVoiceUtterance"
        @hangup="endCall"
        @confirm="submitConfirm({ answers: [] })"
        @interrupt="stopStream"
        @state="onCallState"
      />

      <div v-if="error" class="toast">{{ error }}</div>
    </main>
  </div>
</template>

<style scoped>
.boot {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-2);
  font-size: 14px;
}

.layout {
  display: flex;
  height: 100%;
}

.older-bar {
  display: flex;
  justify-content: center;
  padding: 8px 0 2px;
}

.older-btn {
  padding: 6px 14px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--panel);
  color: var(--text-2);
  font-size: 12px;
}

.older-btn:disabled {
  opacity: 0.6;
}

.main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  position: relative;
}

.topbar {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 52px;
  padding: 0 14px;
  border-bottom: 1px solid var(--border);
  background: var(--panel);
}

.tabs {
  display: flex;
  gap: 4px;
  background: #eef0f3;
  border-radius: 10px;
  padding: 3px;
}

.tab {
  padding: 5px 14px;
  border-radius: 8px;
  font-size: 13px;
  color: var(--text-2);
}

.tab.active {
  background: var(--panel);
  color: var(--text);
  font-weight: 600;
  box-shadow: var(--shadow);
}

.menu-btn {
  display: none;
  width: 34px;
  height: 34px;
  border-radius: 10px;
  font-size: 18px;
}

.menu-btn:active {
  background: rgba(0, 0, 0, 0.06);
}

.call-btn {
  margin-left: auto;
  padding: 6px 12px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: #fff;
  font-size: 13px;
  color: var(--text);
}

.call-btn.active {
  background: #e5484d;
  border-color: #e5484d;
  color: #fff;
}

.title {
  font-weight: 600;
  font-size: 15px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  z-index: 90;
}

.toast {
  position: absolute;
  left: 50%;
  bottom: 90px;
  transform: translateX(-50%);
  max-width: 80%;
  padding: 10px 16px;
  background: rgba(229, 72, 77, 0.95);
  color: #fff;
  font-size: 13px;
  border-radius: 10px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  z-index: 50;
  pointer-events: none;
}

@media (max-width: 768px) {
  .menu-btn {
    display: flex;
    align-items: center;
    justify-content: center;
  }
}
</style>
