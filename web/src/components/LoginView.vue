<script setup>
import { ref } from 'vue'
import { authLogin, authRegister } from '../api'

const emit = defineEmits(['success'])

const mode = ref('login') // login | register
const username = ref('')
const password = ref('')
const displayName = ref('')
const error = ref('')
const busy = ref(false)

const isRegister = () => mode.value === 'register'

function switchMode(next) {
  mode.value = next
  error.value = ''
}

async function submit() {
  if (busy.value) return
  const name = username.value.trim()
  if (!name || !password.value) {
    error.value = '请填写用户名与密码'
    return
  }
  if (isRegister() && password.value.length < 8) {
    error.value = '密码至少 8 位，且需包含字母、数字、符号中的至少两类'
    return
  }

  busy.value = true
  error.value = ''
  try {
    if (isRegister()) {
      await authRegister({ username: name, password: password.value, display_name: displayName.value.trim() })
    }
    const session = await authLogin({ username: name, password: password.value })
    emit('success', session.user)
  } catch (e) {
    error.value = e?.message || '登录失败'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="login-wrap">
    <form class="card" @submit.prevent="submit">
      <div class="brand">🦌 Deer Agent</div>
      <div class="subtitle">{{ isRegister() ? '注册新账号' : '登录以继续你的会话' }}</div>

      <label class="field">
        <span>用户名</span>
        <input v-model="username" autocomplete="username" placeholder="至少 3 个字符" />
      </label>

      <label v-if="isRegister()" class="field">
        <span>昵称（可选）</span>
        <input v-model="displayName" autocomplete="nickname" placeholder="默认与用户名相同" />
      </label>

      <label class="field">
        <span>密码</span>
        <input
          v-model="password"
          type="password"
          :autocomplete="isRegister() ? 'new-password' : 'current-password'"
          placeholder="至少 8 位，含两类字符"
        />
      </label>

      <div v-if="error" class="error">{{ error }}</div>

      <button class="submit" type="submit" :disabled="busy">
        {{ busy ? '请稍候…' : isRegister() ? '注册并登录' : '登录' }}
      </button>

      <div class="switch">
        <template v-if="isRegister()">
          已有账号？<a @click="switchMode('login')">去登录</a>
        </template>
        <template v-else>
          还没有账号？<a @click="switchMode('register')">注册一个</a>
        </template>
      </div>
    </form>
  </div>
</template>

<style scoped>
.login-wrap {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--bg, #f6f7f9);
}

.card {
  width: 100%;
  max-width: 360px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 28px 24px;
  border-radius: 16px;
  background: var(--panel, #fff);
  border: 1px solid var(--border, #e5e7eb);
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.06);
}

.brand {
  font-size: 20px;
  font-weight: 700;
  color: var(--accent, #4f7cff);
  text-align: center;
}

.subtitle {
  text-align: center;
  font-size: 13px;
  color: var(--text-2, #6b7280);
  margin-bottom: 4px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  color: var(--text-2, #6b7280);
}

.field input {
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid var(--border, #e5e7eb);
  background: #fff;
  font-size: 14px;
  color: var(--text, #111827);
  outline: none;
  transition: border-color 0.15s;
}

.field input:focus {
  border-color: var(--accent, #4f7cff);
}

.error {
  padding: 8px 10px;
  border-radius: 8px;
  background: rgba(229, 72, 77, 0.1);
  color: #c03034;
  font-size: 13px;
}

.submit {
  margin-top: 4px;
  padding: 11px;
  border-radius: 10px;
  background: var(--accent, #4f7cff);
  color: #fff;
  font-size: 15px;
  font-weight: 600;
}

.submit:disabled {
  opacity: 0.7;
}

.switch {
  text-align: center;
  font-size: 13px;
  color: var(--text-2, #6b7280);
}

.switch a {
  color: var(--accent, #4f7cff);
  cursor: pointer;
}
</style>
