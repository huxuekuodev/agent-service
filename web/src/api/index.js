/**
 * Deer Agent 前端 API 客户端
 *
 * 后端统一响应信封：{ data, msg, status }
 *   - status === 200 成功，msg 为空
 *   - status >= 1000  业务错误，msg 为提示
 * HTTP 状态码恒为 200，这里统一用业务编码判断成败。
 *
 * 认证（账号密码 + JWT）：
 *   - access token 存 sessionStorage（关闭标签即失效），refresh token 存 localStorage（可续期）；
 *   - 业务码 1201（过期）时用 refresh 静默换新令牌并重放一次原请求（旋转后的 refresh 覆盖存储）；
 *   - 1200/1202（未登录/令牌非法）直接抛错，由页面切到登录视图。
 */

const BASE = '/sessions'
const MONITOR = '/monitor'
const AUTH = '/auth'

// 业务编码（与后端 app/core/response.py 对齐）
export const STATUS = {
  OK: 200,
  SESSION_NOT_FOUND: 1100,
  UNAUTHORIZED: 1200,
  TOKEN_EXPIRED: 1201,
  TOKEN_INVALID: 1202,
  STORE_UNAVAILABLE: 1207,
}

// ---------------------------------------------------------------------------
// 令牌存储
// ---------------------------------------------------------------------------

const ACCESS_KEY = 'dsh.accessToken'
const REFRESH_KEY = 'dsh.refreshToken'
const USER_KEY = 'dsh.user'

function readStore(key) {
  try {
    return sessionStorage.getItem(key) || localStorage.getItem(key) || ''
  } catch {
    return ''
  }
}

function writeStore(key, value, persistent) {
  try {
    if (persistent) localStorage.setItem(key, value)
    else sessionStorage.setItem(key, value)
  } catch {
    /* 隐私模式下写入失败：退化为仅内存态 */
  }
}

function dropStore(key) {
  try {
    localStorage.removeItem(key)
    sessionStorage.removeItem(key)
  } catch {
    /* ignore */
  }
}

export function getAccessToken() {
  return readStore(ACCESS_KEY)
}

export function getRefreshToken() {
  return readStore(REFRESH_KEY)
}

export function getStoredUser() {
  const raw = readStore(USER_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw)
  } catch {
    return null
  }
}

function saveSession(tokens, user) {
  writeStore(ACCESS_KEY, tokens.access_token, false)
  writeStore(REFRESH_KEY, tokens.refresh_token, true)
  if (user) writeStore(USER_KEY, JSON.stringify(user), true)
}

export function clearAuth() {
  dropStore(ACCESS_KEY)
  dropStore(REFRESH_KEY)
  dropStore(USER_KEY)
}

export function hasToken() {
  return Boolean(getAccessToken() || getRefreshToken())
}

// ---------------------------------------------------------------------------
// 请求基础
// ---------------------------------------------------------------------------

function authHeaders(extra = {}) {
  const token = getAccessToken()
  return token ? { Authorization: `Bearer ${token}`, ...extra } : { ...extra }
}

function toError(body) {
  const e = new Error(body?.msg || `业务错误(${body?.status})`)
  e.status = body?.status
  e.data = body?.data
  return e
}

/** 用 refresh token 换新令牌（并发请求共享同一个刷新 Promise，避免旋转竞争） */
let refreshing = null

async function refreshAccessToken() {
  const refresh_token = getRefreshToken()
  if (!refresh_token) throw toError({ status: STATUS.UNAUTHORIZED, msg: '登录已失效，请重新登录' })
  if (!refreshing) {
    refreshing = (async () => {
      const res = await fetch(`${AUTH}/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token }),
      })
      const body = await res.json().catch(() => null)
      if (!body || body.status !== STATUS.OK) {
        clearAuth()
        throw toError(body || { status: STATUS.UNAUTHORIZED, msg: '登录已失效，请重新登录' })
      }
      saveSession(body.data, body.data.user)
      return body.data.access_token
    })().finally(() => {
      refreshing = null
    })
  }
  return refreshing
}

/**
 * 统一请求：自动带令牌，1201 时静默刷新并重放一次。
 *
 * @param {string} path
 * @param {Object} options - fetch 选项；额外支持 `skipAuth`（登录/注册用）
 * @param {string} base
 * @param {boolean} retryOnExpired
 */
async function request(path, options = {}, base = BASE, retryOnExpired = true) {
  const { skipAuth = false, ...init } = options
  const headers = { 'Content-Type': 'application/json', ...(init.headers || {}) }
  const res = await fetch(`${base}${path}`, { ...init, headers: skipAuth ? headers : authHeaders(headers) })
  const body = await res.json().catch(() => null)
  if (!body || typeof body.status !== 'number') {
    throw new Error('服务返回格式异常')
  }
  if (body.status === STATUS.TOKEN_EXPIRED && retryOnExpired && !skipAuth) {
    await refreshAccessToken()
    return request(path, options, base, false)
  }
  if (body.status !== STATUS.OK) {
    throw toError(body)
  }
  return body.data
}

// ---------------------------------------------------------------------------
// 认证
// ---------------------------------------------------------------------------

export async function authRegister({ username, password, display_name = '' }) {
  return request('/register', { method: 'POST', body: JSON.stringify({ username, password, display_name }), skipAuth: true }, AUTH)
}

export async function authLogin({ username, password }) {
  const data = await request('/login', { method: 'POST', body: JSON.stringify({ username, password }), skipAuth: true }, AUTH)
  saveSession(data, data.user)
  return data
}

export async function authMe() {
  const user = await request('/me', {}, AUTH)
  writeStore(USER_KEY, JSON.stringify(user), true)
  return user
}

export async function authLogout(allDevices = false) {
  try {
    await request('/logout', { method: 'POST', body: JSON.stringify({ refresh_token: getRefreshToken(), all_devices: allDevices }) }, AUTH)
  } catch {
    /* 登出失败也要清掉本地登录态 */
  } finally {
    clearAuth()
  }
}

// ---------------------------------------------------------------------------
// 会话
// ---------------------------------------------------------------------------

/** 创建会话 */
export function createSession({ title = '', model_role = null } = {}) {
  return request('', { method: 'POST', body: JSON.stringify({ title, model_role }) })
}

/** 列出会话（键集分页 + 标题搜索） */
export function listSessions({ limit, cursor, q } = {}) {
  const qs = new URLSearchParams()
  if (limit) qs.set('limit', limit)
  if (cursor) qs.set('cursor', cursor)
  if (q) qs.set('q', q)
  const suffix = qs.toString() ? `?${qs}` : ''
  return request(suffix)
}

/** 会话详情 */
export function getSession(sessionId) {
  return request(`/${sessionId}`)
}

/** 重命名 / 归档 */
export function updateSession(sessionId, { title, status } = {}) {
  return request(`/${sessionId}`, { method: 'PATCH', body: JSON.stringify({ title, status }) })
}

/** 删除会话（逻辑删除 + 清理 checkpoint） */
export function deleteSession(sessionId) {
  return request(`/${sessionId}`, { method: 'DELETE' })
}

/** 历史消息（倒序取页、正序返回） */
export function getMessages(sessionId, { limit, before_seq } = {}) {
  const qs = new URLSearchParams()
  if (limit) qs.set('limit', limit)
  if (before_seq) qs.set('before_seq', before_seq)
  const suffix = qs.toString() ? `?${qs}` : ''
  return request(`/${sessionId}/messages${suffix}`)
}

/** 同步对话（等待完整回复） */
export function chatSync(sessionId, message, clientMsgId = null) {
  return request(`/${sessionId}/chat/sync`, {
    method: 'POST',
    body: JSON.stringify({ message, client_msg_id: clientMsgId }),
  })
}

/**
 * SSE 流式请求（/chat 与 /resume 共用）。后端每条事件都是统一信封：{ data, msg, status }
 *   - status 200：data 为业务事件（thinkMessage | plan | step | tool_call | answer | interrupt | end）
 *   - status >= 1000：出错，msg 为提示
 *
 * @param {string} path    相对于 /sessions 的路径
 * @param {Object} body    请求体
 * @param {Object} handlers
 * @param {(event: Object) => void} handlers.onEvent
 * @param {(err: Error) => void} handlers.onError
 * @param {() => void} [handlers.onFinally]
 * @param {AbortSignal} [handlers.signal]
 */
async function streamSse(path, body, { onEvent, onError, onFinally, signal } = {}) {
  const payload = JSON.stringify(body)
  const send = () =>
    fetch(`${BASE}${path}`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: payload,
      signal,
    })

  try {
    let res = await send()
    if (res.status === 401 && !signal?.aborted) {
      // 网关把业务错误落成 401 时：刷新一次再试
      await refreshAccessToken()
      res = await send()
    }
    if (!res.ok || !res.body) {
      throw new Error(`请求失败（HTTP ${res.status}）`)
    }

    const reader = res.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''

    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      // SSE 事件以空行分隔
      let idx
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const raw = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        const line = raw.split('\n').find((l) => l.startsWith('data:'))
        if (!line) continue
        const jsonStr = line.slice(5).trim()
        if (!jsonStr) continue

        let envelope
        try {
          envelope = JSON.parse(jsonStr)
        } catch {
          continue
        }

        if (typeof envelope.status !== 'number') continue

        if (envelope.status !== 200) {
          throw toError(envelope)
        }

        const event = envelope.data ?? {}
        if (event && typeof event === 'object') {
          onEvent?.(event)
        }
      }
    }
  } catch (err) {
    if (err?.name === 'AbortError') {
      // 用户主动中断，不算错误
    } else {
      onError?.(err)
    }
  } finally {
    onFinally?.()
  }
}

/**
 * SSE 流式对话（新一轮用户消息）。
 *
 * @param {string} sessionId
 * @param {string} message
 * @param {Object} handlers onEvent / onError / onFinally / signal / clientMsgId
 */
export function chatStream(sessionId, message, { clientMsgId = null, voice = false, ...handlers } = {}) {
  return streamSse(`/${sessionId}/chat`, { message, client_msg_id: clientMsgId, voice }, handlers)
}

/**
 * 恢复被挂起的中断（interrupt → resume）。
 *
 * 挂起状态下必须用它而不是 chatStream：用新消息调 /chat 不会消费挂起
 * （后端会返回 1103 拦截），用户的答复会被丢弃。
 *
 * @param {string} sessionId
 * @param {Object} payload
 * @param {Array<{id?: string, selected?: string[], custom?: string}>} payload.answers
 * @param {string} [payload.interruptId] 确认卡片对应的中断 id（校验卡片是否过期）
 * @param {string} [payload.clientMsgId]
 * @param {Object} handlers onEvent / onError / onFinally / signal
 */
export function resumeSession(sessionId, { answers = [], interruptId = '', clientMsgId = null, voice = false } = {}, handlers = {}) {
  return streamSse(`/${sessionId}/resume`, { answers, interrupt_id: interruptId, client_msg_id: clientMsgId, voice }, handlers)
}

// ---------------------------------------------------------------------------
// 监控（/monitor/*）
// ---------------------------------------------------------------------------

/** 可用业务名称列表（含默认槽位含义） */
export function monitorGetPages() {
  return request('', {}, MONITOR).then((d) => d?.pages ?? [])
}

/** 数据日志中出现过的模型 */
export function monitorGetModels() {
  return request('/models', {}, MONITOR).then((d) => d?.models ?? [])
}

/** 某 page 的 Ext 槽位含义 */
export function monitorGetFieldMeanings(page) {
  return request(`/field-meanings?page=${encodeURIComponent(page)}`, {}, MONITOR).then(
    (d) => d?.meanings ?? []
  )
}

/** 保存某 page 的 Ext 槽位含义 */
export function monitorPutFieldMeanings(page, meanings) {
  return request('/field-meanings', {
    method: 'PUT',
    body: JSON.stringify({ page, meanings }),
  }, MONITOR)
}

/** 监控组件列表 */
export function monitorListComponents() {
  return request('/components', {}, MONITOR).then((d) => d?.components ?? [])
}

/** 创建监控组件 */
export function monitorCreateComponent(cfg) {
  return request('/components', { method: 'POST', body: JSON.stringify(cfg) }, MONITOR)
}

/** 更新监控组件 */
export function monitorUpdateComponent(id, cfg) {
  return request(`/components/${id}`, { method: 'PUT', body: JSON.stringify(cfg) }, MONITOR)
}

/** 删除监控组件 */
export function monitorDeleteComponent(id) {
  return request(`/components/${id}`, { method: 'DELETE' }, MONITOR)
}

/**
 * 打点数据聚合查询
 * @param {Object} p - page/metric/model/start/end/granularity/stat/group
 */
export function monitorQuery(p) {
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(p)) {
    if (v !== undefined && v !== null && v !== '') qs.set(k, v)
  }
  return request(`/query?${qs.toString()}`, {}, MONITOR)
}

/** 用户 token 消耗汇总（按模型） */
export function monitorTokenUsage(userId) {
  return request(`/token-usage?user_id=${encodeURIComponent(userId)}`, {}, MONITOR).then(
    (d) => d?.usage ?? []
  )
}
