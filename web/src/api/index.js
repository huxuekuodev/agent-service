/**
 * Deer Agent 前端 API 客户端
 *
 * 后端统一响应信封：{ data, msg, status }
 *   - status === 200 成功，msg 为空
 *   - status >= 1000  业务错误，msg 为提示
 * HTTP 状态码恒为 200，这里统一用业务编码判断成败。
 */

const BASE = '/sessions'

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  const body = await res.json().catch(() => null)
  if (!body || typeof body.status !== 'number') {
    throw new Error('服务返回格式异常')
  }
  if (body.status !== 200) {
    const e = new Error(body.msg || `业务错误(${body.status})`)
    e.status = body.status
    e.data = body.data
    throw e
  }
  return body.data
}

/** 创建会话 */
export function createSession(model_name = null) {
  return request('', {
    method: 'POST',
    body: JSON.stringify({ model_name }),
  })
}

/** 列出会话 */
export function listSessions() {
  return request('')
}

/** 删除会话 */
export function deleteSession(sessionId) {
  return request(`/${sessionId}`, { method: 'DELETE' })
}

/** 同步对话（等待完整回复） */
export async function chatSync(sessionId, message) {
  const data = await request(`/${sessionId}/chat/sync`, {
    method: 'POST',
    body: JSON.stringify({ message, stream: true }),
  })
  return data
}

/**
 * SSE 流式对话。
 * 后端每条事件都是统一信封：{ data, msg, status }
 *   - status 200：data 为业务事件（type: thinkMessage | values | messages | end）
 *   - status >= 1000：出错，msg 为提示
 *
 * @param {string} sessionId
 * @param {string} message
 * @param {Object} handlers
 * @param {(event: Object) => void} handlers.onEvent  每条业务事件
 * @param {(err: Error) => void} handlers.onError
 * @param {() => void} [handlers.onFinally]
 * @param {AbortSignal} [signal]
 */
export async function chatStream(sessionId, message, { onEvent, onError, onFinally, signal } = {}) {
  try {
    const res = await fetch(`${BASE}/${sessionId}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, stream: true }),
      signal,
    })

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
          throw new Error(envelope.msg || `业务错误(${envelope.status})`)
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
