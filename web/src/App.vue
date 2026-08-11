<script setup>
import { ref, onMounted, nextTick } from 'vue'
import Sidebar from './components/Sidebar.vue'
import ChatArea from './components/ChatArea.vue'
import {
  createSession,
  listSessions,
  deleteSession,
  chatStream,
} from './api'

const sessions = ref([])
const currentId = ref(null)
const messages = ref([])
const thinking = ref('')
const streaming = ref(false)
const sidebarOpen = ref(false)
const error = ref('')

const THINK = 'thinkMessage'

// 会话标题：取第一条用户消息
function titleOf(session) {
  return (session?.title || '').slice(0, 20) || '新会话'
}

async function refreshSessions() {
  try {
    const data = await listSessions()
    // 后端当前返回空列表（会话注册表未持久化），前端用本地缓存兜底
    const remote = Array.isArray(data?.sessions) ? data.sessions : []
    const remoteIds = new Set(remote.map((s) => s.id))
    const local = sessions.value.filter((s) => !remoteIds.has(s.id))
    sessions.value = [...local, ...remote]
  } catch (e) {
    // 列表失败不阻塞使用
    console.warn('刷新会话列表失败:', e)
  }
}

async function handleCreate() {
  try {
    const data = await createSession()
    const id = data?.session_id
    if (!id) throw new Error('创建会话未返回 session_id')
    const session = { id, title: '', createdAt: Date.now() }
    sessions.value.unshift(session)
    await switchSession(id)
    sidebarOpen.value = false
  } catch (e) {
    error.value = e.message || '创建会话失败'
  }
}

async function switchSession(id) {
  currentId.value = id
  messages.value = []
  thinking.value = ''
  error.value = ''
  // 切会话清空；如后端按 thread 恢复历史，可在此调用历史接口
}

async function handleDelete(session) {
  const ok = window.confirm(`确定删除会话「${titleOf(session)}」吗？`)
  if (!ok) return
  try {
    await deleteSession(session.id)
    sessions.value = sessions.value.filter((s) => s.id !== session.id)
    if (currentId.value === session.id) {
      currentId.value = null
      messages.value = []
    }
  } catch (e) {
    error.value = e.message || '删除会话失败'
  }
}

async function handleSend(text) {
  if (!currentId.value) {
    // 无会话时先自动创建
    try {
      const data = await createSession()
      const id = data?.session_id
      if (!id) throw new Error('创建会话未返回 session_id')
      sessions.value.unshift({ id, title: '', createdAt: Date.now() })
      currentId.value = id
    } catch (e) {
      error.value = e.message || '创建会话失败'
      return
    }
  }

  // 追加用户消息，并更新会话标题
  messages.value.push({ role: 'user', content: text })
  const s = sessions.value.find((x) => x.id === currentId.value)
  if (s && !s.title) s.title = text

  // 准备助手占位
  messages.value.push({ role: 'assistant', content: '', avatar: '🦌' })
  const aiIndex = messages.value.length - 1

  thinking.value = ''
  error.value = ''
  streaming.value = true
  await nextTick()

  let gotEnd = false

  try {
    await chatStream(
      currentId.value,
      text,
      {
        onEvent(event) {
          const type = event.type

          if (type === 'custom') {
            // thinkMessage 等业务事件嵌套在 event.data 里
            const inner = event.data || {}
            if (inner.type === THINK || inner.type === 'thinking') {
              thinking.value = inner.messages || inner.content || '思考中…'
            }
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
    if (!messages.value[aiIndex]?.content) {
      messages.value[aiIndex].content = e.message || '对话失败'
    }
  }

  // 兜底：后端若未发出 end，也认为流结束
  if (!gotEnd && !messages.value[aiIndex]?.content) {
    messages.value[aiIndex].content = '(无回复)'
  }
}

onMounted(() => {
  refreshSessions()
})
</script>

<template>
  <div class="layout">
    <Sidebar
      :sessions="sessions"
      :current-id="currentId"
      :open="sidebarOpen"
      @create="handleCreate"
      @select="switchSession"
      @delete="handleDelete"
      @close="sidebarOpen = false"
    />

    <!-- 手机端遮罩 -->
    <div v-if="sidebarOpen" class="mask" @click="sidebarOpen = false" />

    <main class="main">
      <header class="topbar">
        <button class="menu-btn" aria-label="菜单" @click="sidebarOpen = true">☰</button>
        <span class="title">{{ titleOf(sessions.find((s) => s.id === currentId)) }}</span>
      </header>

      <ChatArea
        :messages="messages"
        :thinking="thinking"
        :streaming="streaming"
        @send="handleSend"
      />

      <div v-if="error" class="toast">{{ error }}</div>
    </main>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  height: 100%;
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
