<script setup>
import { ref, watch, nextTick } from 'vue'

const props = defineProps({
  messages: { type: Array, default: () => [] },
  thinking: { type: String, default: '' },
  streaming: { type: Boolean, default: false },
})

const emit = defineEmits(['send', 'stop'])

const input = ref('')
const listEl = ref(null)

function onSend() {
  const text = input.value.trim()
  if (!text || props.streaming) return
  emit('send', text)
  input.value = ''
}

function onKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    onSend()
  }
}

// 新消息时滚到底部
watch(
  () => [props.messages.length, props.messages.at(-1)?.content, props.thinking],
  async () => {
    await nextTick()
    if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight
  },
  { deep: true }
)
</script>

<template>
  <div class="chat">
    <div ref="listEl" class="msg-list">
      <div v-if="messages.length === 0" class="empty">
        <div class="empty-logo">🦌</div>
        <p class="empty-title">Deer Agent</p>
        <p class="empty-sub">有什么可以帮你的？</p>
      </div>

      <div
        v-for="(m, i) in messages"
        :key="i"
        class="msg"
        :class="m.role"
      >
        <div class="avatar" v-if="m.role === 'assistant'">{{ m.avatar }}</div>
        <div class="bubble">
          <div class="bubble-text">{{ m.content }}</div>
        </div>
      </div>

      <div v-if="thinking" class="think-bubble">
        <span class="spinner" /> {{ thinking }}
      </div>
    </div>

    <div class="composer">
      <div class="input-wrap">
        <textarea
          v-model="input"
          rows="1"
          placeholder="输入消息，Enter 发送 / Shift+Enter 换行"
          :disabled="streaming"
          @keydown="onKeydown"
        ></textarea>
        <button class="send-btn" :disabled="!input.trim() || streaming" @click="onSend">
          {{ streaming ? '…' : '➤' }}
        </button>
      </div>
      <p class="tip">AI 生成内容仅供参考</p>
    </div>
  </div>
</template>

<style scoped>
.chat {
  display: flex;
  flex-direction: column;
  height: 100%;
  width: 100%;
  max-width: 860px;
  margin: 0 auto;
}

.msg-list {
  flex: 1;
  overflow-y: auto;
  padding: 20px 16px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  scroll-behavior: smooth;
}

.empty {
  margin: auto;
  text-align: center;
  color: var(--text-2);
}

.empty-logo {
  font-size: 52px;
}

.empty-title {
  font-size: 20px;
  font-weight: 700;
  margin: 8px 0 4px;
  color: var(--text);
}

.empty-sub {
  font-size: 14px;
}

.msg {
  display: flex;
  gap: 10px;
  max-width: 100%;
}

.msg.user {
  flex-direction: row-reverse;
}

.avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  background: #eef1f7;
}

.bubble {
  max-width: 78%;
  padding: 10px 14px;
  border-radius: var(--radius);
  font-size: 15px;
  word-break: break-word;
  white-space: pre-wrap;
}

.msg.user .bubble {
  background: var(--user-bubble);
  color: var(--user-text);
  border-bottom-right-radius: 4px;
}

.msg.assistant .bubble {
  background: var(--assistant-bubble);
  border: 1px solid var(--border);
  border-bottom-left-radius: 4px;
  box-shadow: var(--shadow);
}

.think-bubble {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  align-self: flex-start;
  padding: 8px 14px;
  background: #f0f3fa;
  color: var(--text-2);
  border-radius: var(--radius);
  font-size: 13px;
}

.spinner {
  width: 12px;
  height: 12px;
  border: 2px solid #c3cbe2;
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
  flex-shrink: 0;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

.composer {
  padding: 8px 12px calc(12px + env(safe-area-inset-bottom));
}

.input-wrap {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 8px 8px 8px 14px;
  box-shadow: var(--shadow);
}

.input-wrap:focus-within {
  border-color: var(--accent);
}

textarea {
  flex: 1;
  resize: none;
  border: none;
  background: transparent;
  max-height: 120px;
  line-height: 1.5;
  padding: 6px 0;
}

textarea:disabled {
  opacity: 0.6;
}

.send-btn {
  width: 36px;
  height: 36px;
  border-radius: 12px;
  background: var(--accent);
  color: #fff;
  font-size: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: background 0.15s;
}

.send-btn:disabled {
  background: #c3cbe2;
  cursor: not-allowed;
}

.send-btn:active:not(:disabled) {
  background: var(--accent-dark);
}

.tip {
  text-align: center;
  font-size: 12px;
  color: #b6bcc6;
  margin: 6px 0 0;
}
</style>
