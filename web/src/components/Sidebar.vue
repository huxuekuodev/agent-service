<script setup>
defineProps({
  sessions: { type: Array, default: () => [] },
  currentId: { type: String, default: null },
  open: { type: Boolean, default: false },
})

const emit = defineEmits(['create', 'select', 'delete', 'close'])
</script>

<template>
  <div class="sidebar" :class="{ open }">
    <div class="sidebar-header">
      <span class="brand">Deer Agent</span>
      <button class="icon-btn close-btn" aria-label="关闭" @click="emit('close')">✕</button>
    </div>

    <button class="new-btn" @click="emit('create')">
      <span class="plus">＋</span> 新建会话
    </button>

    <div class="list">
      <div
        v-for="s in sessions"
        :key="s.id"
        class="item"
        :class="{ active: s.id === currentId }"
        @click="emit('select', s.id)"
      >
        <span class="item-title">{{ s.title || '新会话' }}</span>
        <button
          class="icon-btn del-btn"
          aria-label="删除会话"
          @click.stop="emit('delete', s)"
        >
          🗑
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.sidebar {
  display: flex;
  flex-direction: column;
  width: 260px;
  flex-shrink: 0;
  background: var(--panel);
  border-right: 1px solid var(--border);
  height: 100%;
}

.sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px;
}

.brand {
  font-weight: 700;
  font-size: 17px;
  color: var(--accent);
}

.close-btn {
  display: none;
}

.new-btn {
  margin: 0 12px 12px;
  padding: 10px;
  border-radius: var(--radius);
  background: var(--accent);
  color: #fff;
  font-size: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  transition: background 0.15s;
}

.new-btn:active {
  background: var(--accent-dark);
}

.plus {
  font-size: 16px;
}

.list {
  flex: 1;
  overflow-y: auto;
  padding: 0 8px;
}

.item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 10px 10px 12px;
  margin-bottom: 2px;
  border-radius: 10px;
  cursor: pointer;
  transition: background 0.12s;
}

.item:hover {
  background: #f2f4f7;
}

.item.active {
  background: #eaf0ff;
  color: var(--accent);
  font-weight: 600;
}

.item-title {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 14px;
}

.del-btn {
  opacity: 0;
  transition: opacity 0.12s;
}

.item:hover .del-btn {
  opacity: 1;
}

.icon-btn {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
}

.icon-btn:active {
  background: rgba(0, 0, 0, 0.06);
}

/* 手机端：抽屉式侧边栏 */
@media (max-width: 768px) {
  .sidebar {
    position: fixed;
    top: 0;
    left: 0;
    bottom: 0;
    width: 280px;
    z-index: 100;
    transform: translateX(-100%);
    transition: transform 0.25s ease;
    box-shadow: 4px 0 20px rgba(0, 0, 0, 0.1);
  }

  .sidebar.open {
    transform: translateX(0);
  }

  .close-btn {
    display: flex;
  }
}
</style>
