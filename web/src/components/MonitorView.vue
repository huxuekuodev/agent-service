<script setup>
import { computed, ref } from 'vue'
import {
  monitorQuery,
  monitorGetPages,
  monitorGetModels,
  monitorListComponents,
  monitorCreateComponent,
  monitorUpdateComponent,
  monitorDeleteComponent,
  monitorGetFieldMeanings,
  monitorPutFieldMeanings,
  monitorTokenUsage,
} from '../api'

const props = defineProps({
  sessionId: { type: String, default: '' },
})

// ---------------------------------------------------------------------------
// 元信息
// ---------------------------------------------------------------------------
const pages = ref([]) // [{page, default_meanings}]
const models = ref([])
const fieldMeanings = ref([]) // 当前编辑页的含义
const meaningPage = ref('call_model')
const showMeanings = ref(false)

// ---------------------------------------------------------------------------
// 时间范围
// ---------------------------------------------------------------------------
const RANGE_OPTIONS = [
  { value: '30', label: '近30天' },
  { value: '7', label: '近7天' },
  { value: 'yesterday', label: '昨天' },
  { value: 'custom', label: '自定义' },
]
const rangeType = ref('30')
const startInput = ref('')
const endInput = ref('')

function toISOLocal(d) {
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}${d.getTimezoneOffset() <= 0 ? '+' : '-'}${pad(Math.abs(d.getTimezoneOffset() / 60))}:${pad(Math.abs(d.getTimezoneOffset() % 60))}`
}

const range = computed(() => {
  const now = new Date()
  let start, end
  if (rangeType.value === 'custom') {
    start = new Date(startInput.value)
    end = new Date(endInput.value)
    if (isNaN(start) || isNaN(end) || start > end) return null
    return { start: toISOLocal(start), end: toISOLocal(end) }
  }
  end = now
  if (rangeType.value === 'yesterday') {
    start = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1, 0, 0, 0)
    end = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0) - 1
    end = new Date(end)
  } else {
    start = new Date(now.getTime() - Number(rangeType.value) * 86400000)
  }
  return { start: toISOLocal(start), end: toISOLocal(end) }
})

// ---------------------------------------------------------------------------
// 组件配置
// ---------------------------------------------------------------------------
const components = ref([])
const form = ref(null) // null=关闭；{} = 新增；{id,...} = 编辑
const seriesCache = ref({})
const loading = ref(false)
const error = ref('')

function defaultForm() {
  return {
    name: '',
    page: 'call_model',
    metric: 'p0',
    model: '',
    stat: 'sum',
    granularity: 'minute',
    range_type: rangeType.value,
  }
}

async function loadMeta() {
  try {
    const p = await monitorGetPages()
    pages.value = p
    if (!pages.value.find((x) => x.page === meaningPage.value) && p.length) {
      meaningPage.value = p[0].page
    }
    models.value = await monitorGetModels()
  } catch (e) {
    console.warn('加载监控元信息失败:', e)
  }
}

async function loadComponents() {
  try {
    components.value = await monitorListComponents()
    await Promise.all(components.value.map((c) => loadSeries(c)))
  } catch (e) {
    error.value = e.message || '加载组件失败'
  }
}

async function loadSeries(c) {
  const r = range.value
  if (!r) return
  try {
    const data = await monitorQuery({
      page: c.page,
      metric: c.metric || 'p0',
      model: c.model || '',
      start: r.start,
      end: r.end,
      granularity: c.granularity || 'minute',
      stat: c.stat || 'sum',
      group: c.model ? 'none' : 'model',
    })
    seriesCache.value[c.id] = data
  } catch (e) {
    seriesCache.value[c.id] = null
    console.warn('查询失败:', c.name, e)
  }
}

function openAdd() {
  form.value = defaultForm()
}

function openEdit(c) {
  form.value = {
    id: c.id,
    name: c.name,
    page: c.page,
    metric: c.metric,
    model: c.model || '',
    stat: c.stat,
    granularity: c.granularity,
    range_type: c.range_type || '30',
  }
}

async function saveForm() {
  const f = form.value
  if (!f.name || !f.page) {
    error.value = '组件名与业务名称必填'
    return
  }
  const payload = {
    name: f.name,
    page: f.page,
    metric: f.metric,
    model: f.model || null,
    stat: f.stat,
    granularity: f.granularity,
    range_type: f.range_type,
  }
  try {
    if (f.id) {
      await monitorUpdateComponent(f.id, payload)
    } else {
      await monitorCreateComponent(payload)
    }
    form.value = null
    await loadComponents()
  } catch (e) {
    error.value = e.message || '保存组件失败'
  }
}

async function removeComponent(c) {
  if (!window.confirm(`删除组件「${c.name}」？`)) return
  try {
    await monitorDeleteComponent(c.id)
    delete seriesCache.value[c.id]
    components.value = components.value.filter((x) => x.id !== c.id)
  } catch (e) {
    error.value = e.message || '删除失败'
  }
}

// ---------------------------------------------------------------------------
// 字段含义
// ---------------------------------------------------------------------------
async function openMeanings() {
  showMeanings.value = true
  await loadMeanings()
}

async function loadMeanings() {
  try {
    fieldMeanings.value = await monitorGetFieldMeanings(meaningPage.value)
  } catch (e) {
    error.value = e.message || '加载字段含义失败'
  }
}

async function saveMeanings() {
  try {
    await monitorPutFieldMeanings(meaningPage.value, fieldMeanings.value)
    showMeanings.value = false
  } catch (e) {
    error.value = e.message || '保存字段含义失败'
  }
}

// ---------------------------------------------------------------------------
// Token 消耗汇总（当前会话）
// ---------------------------------------------------------------------------
const tokenUsage = ref([])

async function loadTokenUsage() {
  if (!props.sessionId) {
    tokenUsage.value = []
    return
  }
  try {
    tokenUsage.value = await monitorTokenUsage(props.sessionId)
  } catch (e) {
    console.warn('加载 token 汇总失败:', e)
  }
}

// ---------------------------------------------------------------------------
// 渲染辅助
// ---------------------------------------------------------------------------
function pageLabel(page) {
  return page || '-'
}

function meaningOf(page, slot) {
  const p = pages.value.find((x) => x.page === page)
  const m = p?.default_meanings?.find((x) => x.slot === slot)
  return m?.label || ''
}

function refreshAll() {
  loadComponents()
  loadTokenUsage()
}

defineExpose({ refreshAll })

// 初始化
loadMeta()
loadComponents()
loadTokenUsage()
</script>

<template>
  <div class="monitor">
    <header class="m-top">
      <h2 class="m-title">📈 监控</h2>

      <div class="range-box">
        <select v-model="rangeType" class="ctl" @change="loadComponents">
          <option v-for="o in RANGE_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
        </select>
        <template v-if="rangeType === 'custom'">
          <input v-model="startInput" type="datetime-local" class="ctl" />
          <span class="sep">~</span>
          <input v-model="endInput" type="datetime-local" class="ctl" />
          <button class="btn" @click="loadComponents">查询</button>
        </template>
      </div>

      <div class="top-actions">
        <button class="btn primary" @click="openAdd">＋ 添加组件</button>
        <button class="btn" @click="openMeanings">字段含义</button>
      </div>
    </header>

    <div v-if="error" class="m-err">{{ error }}</div>

    <!-- 添加/编辑组件表单 -->
    <div v-if="form" class="form-card">
      <div class="form-title">{{ form.id ? '编辑组件' : '添加组件' }}</div>
      <div class="form-grid">
        <label>组件名 <input v-model="form.name" class="ctl" placeholder="如：模型耗时对比" /></label>
        <label>业务名称(page)
          <select v-model="form.page" class="ctl">
            <option v-for="p in pages" :key="p.page" :value="p.page">{{ p.page }}</option>
          </select>
        </label>
        <label>监控指标
          <select v-model="form.metric" class="ctl">
            <option v-for="i in 15" :key="i" :value="`p${i - 1}`">
              p{{ i - 1 }} {{ meaningOf(form.page, `p${i - 1}`) ? '· ' + meaningOf(form.page, `p${i - 1}`) : '' }}
            </option>
          </select>
        </label>
        <label>模型过滤
          <select v-model="form.model" class="ctl">
            <option value="">全部（按模型分组对比）</option>
            <option v-for="m in models" :key="m" :value="m">{{ m }}</option>
          </select>
        </label>
        <label>统计方式
          <select v-model="form.stat" class="ctl">
            <option value="sum">和值</option>
            <option value="avg">平均值</option>
          </select>
        </label>
        <label>展示粒度
          <select v-model="form.granularity" class="ctl">
            <option value="minute">分钟级</option>
            <option value="hour">小时级</option>
          </select>
        </label>
      </div>
      <div class="form-actions">
        <button class="btn primary" @click="saveForm">保存</button>
        <button class="btn" @click="form = null">取消</button>
      </div>
    </div>

    <!-- 组件网格 -->
    <div class="grid">
      <div v-for="c in components" :key="c.id" class="card">
        <div class="card-head">
          <span class="card-name">{{ c.name }}</span>
          <span class="card-tags">
            <span class="tag">{{ c.page }}</span>
            <span class="tag">{{ c.metric }} {{ meaningOf(c.page, c.metric) }}</span>
            <span class="tag">{{ c.stat === 'sum' ? '和值' : '平均' }}</span>
            <span class="tag">{{ c.granularity === 'minute' ? '分钟' : '小时' }}</span>
            <span v-if="c.model" class="tag model">{{ c.model }}</span>
          </span>
          <span class="card-ops">
            <button class="mini" @click="openEdit(c)" title="编辑">✎</button>
            <button class="mini" @click="removeComponent(c)" title="删除">🗑</button>
          </span>
        </div>

        <MonitorChart :data="seriesCache[c.id]" :height="160" />

        <div v-if="seriesCache[c.id]" class="card-foot">
          记录 {{ seriesCache[c.id].total.count }} 条 · 和 {{ seriesCache[c.id].total.sum }} · 平均
          {{ seriesCache[c.id].total.avg }}
          <button class="mini" @click="loadSeries(c)" title="刷新">⟳</button>
        </div>
        <div v-else class="card-foot muted">暂无数据（请确认时间范围内有打点）</div>
      </div>

      <div v-if="!components.length && !loading" class="empty-tip">
        还没有监控组件，点击「＋ 添加组件」创建第一个组件。
      </div>
    </div>

    <!-- Token 消耗汇总 -->
    <section class="token-card">
      <h3>💳 Token 消耗汇总（当前会话）</h3>
      <p v-if="!sessionId" class="muted">创建/进入会话后可查看该会话（用户）的累计 token 消耗。</p>
      <table v-else-if="tokenUsage.length">
        <thead>
          <tr><th>模型</th><th>输入 token</th><th>输出 token</th><th>总 token</th><th>费用(元)</th></tr>
        </thead>
        <tbody>
          <tr v-for="u in tokenUsage" :key="u.model">
            <td>{{ u.model }}</td>
            <td>{{ u.input_tokens }}</td>
            <td>{{ u.output_tokens }}</td>
            <td>{{ u.total_tokens }}</td>
            <td>{{ u.cost }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">该会话暂无 token 记录。</p>
    </section>

    <!-- 字段含义编辑弹层 -->
    <div v-if="showMeanings" class="mask" @click.self="showMeanings = false">
      <div class="dialog">
        <div class="dialog-head">
          <h3>Ext 字段含义配置</h3>
          <select v-model="meaningPage" class="ctl" @change="loadMeanings">
            <option v-for="p in pages" :key="p.page" :value="p.page">{{ p.page }}</option>
          </select>
        </div>
        <p class="muted">为 {{ meaningPage }} 业务配置 P0-P14 各槽位含义（展示在监控组件上）。</p>
        <div class="meaning-list">
          <div v-for="m in fieldMeanings" :key="m.slot" class="meaning-row">
            <span class="slot">{{ m.slot }}</span>
            <input v-model="m.label" class="ctl" placeholder="含义/展示名" />
            <input v-model="m.description" class="ctl grow" placeholder="详细说明（可选）" />
          </div>
        </div>
        <div class="form-actions">
          <button class="btn primary" @click="saveMeanings">保存</button>
          <button class="btn" @click="showMeanings = false">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import MonitorChart from './MonitorChart.vue'
export default { components: { MonitorChart } }
</script>

<style scoped>
.monitor {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  padding: 16px;
}
.m-top {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.m-title {
  margin: 0;
  font-size: 18px;
}
.range-box {
  display: flex;
  align-items: center;
  gap: 6px;
}
.sep {
  color: var(--text-2);
}
.top-actions {
  margin-left: auto;
  display: flex;
  gap: 8px;
}
.btn {
  padding: 6px 12px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--panel);
  font-size: 13px;
}
.btn.primary {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}
.ctl {
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel);
  font-size: 13px;
}
.m-err {
  background: rgba(229, 72, 77, 0.1);
  color: var(--error);
  padding: 8px 12px;
  border-radius: 8px;
  margin-bottom: 12px;
  font-size: 13px;
}
.form-card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 14px;
  margin-bottom: 14px;
}
.form-title {
  font-weight: 600;
  margin-bottom: 10px;
}
.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 10px;
}
.form-grid label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--text-2);
}
.form-actions {
  margin-top: 12px;
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(420px, 1fr));
  gap: 14px;
}
.card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 12px;
  box-shadow: var(--shadow);
}
.card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.card-name {
  font-weight: 600;
  font-size: 14px;
}
.card-tags {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}
.tag {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 6px;
  background: #eef1fb;
  color: var(--accent-dark);
}
.tag.model {
  background: #e8f5ee;
  color: #1a7f4e;
}
.card-ops {
  margin-left: auto;
  display: flex;
  gap: 4px;
}
.mini {
  padding: 2px 6px;
  border-radius: 6px;
  font-size: 12px;
}
.mini:hover {
  background: rgba(0, 0, 0, 0.05);
}
.card-foot {
  margin-top: 8px;
  font-size: 12px;
  color: var(--text-2);
  display: flex;
  align-items: center;
  gap: 8px;
}
.muted {
  color: var(--text-2);
  font-size: 13px;
}
.empty-tip {
  grid-column: 1 / -1;
  text-align: center;
  color: var(--text-2);
  padding: 40px 0;
}
.token-card {
  margin-top: 18px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 14px;
}
.token-card h3 {
  margin: 0 0 10px;
  font-size: 15px;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
th,
td {
  text-align: left;
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
}
th {
  color: var(--text-2);
  font-weight: 500;
}
.mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  z-index: 90;
  display: flex;
  align-items: center;
  justify-content: center;
}
.dialog {
  background: var(--panel);
  border-radius: var(--radius);
  padding: 16px;
  width: min(680px, 92vw);
  max-height: 86vh;
  overflow-y: auto;
}
.dialog-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.dialog-head h3 {
  margin: 0;
}
.meaning-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 10px 0;
}
.meaning-row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.slot {
  width: 34px;
  font-size: 12px;
  color: var(--text-2);
  flex-shrink: 0;
}
.grow {
  flex: 1;
}
@media (max-width: 768px) {
  .grid {
    grid-template-columns: 1fr;
  }
  .top-actions {
    margin-left: 0;
  }
}
</style>
