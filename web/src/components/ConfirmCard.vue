<script setup>
import { ref } from 'vue'

/**
 * 人工确认卡片：渲染 interrupt 载荷（app/agents/interrupts.py 的 build_ask 结构）。
 *
 * 当前交互（按产品要求）：**只展示步骤，不让用户选步骤**，但用户可以留一句意见；
 * 留空 = 照此计划执行，填了意见 = 交回规划节点重新规划。
 */
const props = defineProps({
  payload: { type: Object, required: true },
  busy: { type: Boolean, default: false },
})

const emit = defineEmits(['submit'])

const feedback = ref('')

const questions = () => props.payload?.questions || []
const items = () => props.payload?.items || []
const allowFeedback = () =>
  questions().some((q) => q.allow_custom !== false) || questions().length === 0
const placeholder = () =>
  questions().find((q) => q.custom_placeholder)?.custom_placeholder || '例如：把北京换成上海'
const prompt = () => questions().find((q) => q.question)?.question || '需要调整吗？'

function submit(withFeedback) {
  if (props.busy) return
  const text = withFeedback ? feedback.value.trim() : ''
  emit('submit', { answers: [{ id: questions()[0]?.id || 'review', selected: [], custom: text }] })
  feedback.value = ''
}
</script>

<template>
  <div class="confirm-card">
    <div class="head">
      <span class="badge">待确认</span>
      <span class="title">{{ payload.title || '计划已生成' }}</span>
    </div>

    <div v-if="payload.content" class="content">{{ payload.content }}</div>

    <ol class="steps">
      <li v-for="(it, i) in items()" :key="it.plan_id || i">
        <span class="step-name">{{ it.name || it.plan_id }}</span>
        <span v-if="it.desc" class="step-desc">{{ it.desc }}</span>
      </li>
    </ol>

    <div v-if="allowFeedback()" class="feedback">
      <div class="prompt">{{ prompt() }}</div>
      <textarea
        v-model="feedback"
        rows="2"
        :placeholder="placeholder()"
        :disabled="busy"
        @keydown.enter.exact.prevent="feedback.trim() && submit(true)"
      />
    </div>

    <div class="actions">
      <button class="btn primary" :disabled="busy" @click="submit(false)">
        {{ busy ? '处理中…' : '继续执行' }}
      </button>
      <button v-if="allowFeedback()" class="btn" :disabled="busy || !feedback.trim()" @click="submit(true)">
        提交意见并重排
      </button>
    </div>
  </div>
</template>

<style scoped>
.confirm-card {
  margin: 8px 12px 4px;
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--panel);
  box-shadow: var(--shadow);
}

.head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.badge {
  padding: 1px 8px;
  border-radius: 999px;
  background: #fff4e5;
  color: #b26a00;
  font-size: 12px;
}

.title {
  font-weight: 600;
  font-size: 14px;
}

.content {
  font-size: 13px;
  color: var(--text-2);
  margin-bottom: 6px;
}

.steps {
  margin: 0 0 10px;
  padding-left: 20px;
  max-height: 220px;
  overflow-y: auto;
}

.steps li {
  font-size: 13px;
  line-height: 1.7;
}

.step-name {
  font-weight: 600;
}

.step-desc {
  display: block;
  color: var(--text-2);
  font-size: 12px;
}

.feedback {
  margin-bottom: 10px;
}

.prompt {
  font-size: 12px;
  color: var(--text-2);
  margin-bottom: 4px;
}

.feedback textarea {
  width: 100%;
  resize: vertical;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 13px;
  font-family: inherit;
  color: var(--text);
  background: #fff;
  outline: none;
}

.feedback textarea:focus {
  border-color: var(--accent);
}

.actions {
  display: flex;
  gap: 8px;
}

.btn {
  padding: 7px 14px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: #fff;
  font-size: 13px;
  color: var(--text);
}

.btn.primary {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
  font-weight: 600;
}

.btn:disabled {
  opacity: 0.55;
}
</style>
