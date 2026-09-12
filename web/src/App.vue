<script setup>
import { ref, onMounted, nextTick } from 'vue'
import Sidebar from './components/Sidebar.vue'
import ChatArea from './components/ChatArea.vue'
import MonitorView from './components/MonitorView.vue'
import LoginView from './components/LoginView.vue'
import {
  authMe,
  authLogout,
  createSession,
  getMessages,
  hasToken,
  getStoredUser,
  clearAuth,
  listSessions,
  deleteSession,
  chatStream,
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
}

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

  // 准备助手占位
  messages.value.push({ role: 'assistant', content: '', avatar: '🦌' })
  const aiIndex = messages.value.length - 1

  thinking.value = ''
  progress.value = []
  error.value = ''
  streaming.value = true
  await nextTick()

  let gotEnd = false

  try {
    await chatStream(
      currentId.value,
      text,
      {
        clientMsgId: newClientMsgId(),
        onEvent(event) {
          const type = event.type

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
              if (text) {
                messages.value[aiIndex].content = messages.value[aiIndex].content
                  ? messages.value[aiIndex].content
                  : text
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
              const content =
                typeof item === 'string' ? item : item?.content ?? ''
              if (typeof content === 'string' && content.trim()) {
                messages.value[aiIndex].content += content
              }
            }
            return
          }

          if (type === 'values') {
            // 完整状态快照：planner-execute 模式的最终答案由「新增的 AIMessage」承载
            // （见 plan_model_node return {"messages": [answer_msg], "completed": True}），
            // 从后往前找最后一个非空的 AI 回复；跳过 ToolMessage 等内部消息
            // （避免 "Returning structured response: ..." dump 上屏）。
            const data = event.data || {}
            const msgList = data.messages
            if (Array.isArray(msgList) && !messages.value[aiIndex]?.content) {
              for (let i = msgList.length - 1; i >= 0; i--) {
                const m = msgList[i]
                if (!m || typeof m.type !== 'string') continue
                if (m.type === 'HumanMessage') break // 越过用户消息即止
                if (m.type !== 'AIMessage') continue // 跳过 ToolMessage 等内部消息
                const c = m.content
                if (typeof c === 'string' && c.trim()) {
                  messages.value[aiIndex].content = c
                  break
                }
              }
            }
            return
          }

          if (type === 'end') {
            gotEnd = true
          }
        },
        onError(err) {
          if (handleAuthError(err)) return
          error.value = err?.message || '对话失败'
          if (!messages.value[aiIndex]?.content) {
            messages.value[aiIndex].content = error.value
          }
        },
        onFinally() {
          streaming.value = false
          thinking.value = ''
        },
      }
    )
  } catch (e) {
    streaming.value = false
    thinking.value = ''
    if (!handleAuthError(e) && !messages.value[aiIndex]?.content) {
      messages.value[aiIndex].content = e.message || '对话失败'
    }
  }

  // 兜底：后端若未发出 end，也认为流结束
  if (!gotEnd && !messages.value[aiIndex]?.content) {
    messages.value[aiIndex].content = '(无回复)'
  }

  // 流结束后同步列表（服务端已落库：标题、最后消息预览、消息数）
  if (user.value) await refreshSessions()
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
          :streaming="streaming"
          @send="handleSend"
        />
      </template>

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
