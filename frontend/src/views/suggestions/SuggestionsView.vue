<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useProjectStore } from '../../stores/project'
import {
  getSuggestions,
  generateSuggestions,
  resolveSuggestion,
  deleteSuggestion,
  type Suggestion,
} from '../../api/client'

const store = useProjectStore()
const suggestions = ref<Suggestion[]>([])
const loading = ref(false)
const generating = ref(false)
const filterCategory = ref('all')
const filterStatus = ref<'all' | 'pending' | 'resolved'>('all')

const categoryLabels: Record<string, string> = {
  content_optimization: '内容优化',
  seo_strategy: 'SEO策略',
  platform_focus: '平台重点',
  competitive_strategy: '竞品策略',
}

const priorityLabels: Record<string, { label: string; class: string }> = {
  high: { label: '高', class: 'priority-high' },
  medium: { label: '中', class: 'priority-medium' },
  low: { label: '低', class: 'priority-low' },
}

const filtered = computed(() => {
  let list = suggestions.value
  if (filterCategory.value !== 'all') {
    list = list.filter(s => s.category === filterCategory.value)
  }
  if (filterStatus.value === 'pending') {
    list = list.filter(s => !s.is_resolved)
  } else if (filterStatus.value === 'resolved') {
    list = list.filter(s => s.is_resolved)
  }
  return list
})

// Group by category for display
const grouped = computed(() => {
  const groups: Record<string, Suggestion[]> = {}
  for (const s of filtered.value) {
    const cat = s.category
    if (!groups[cat]) groups[cat] = []
    groups[cat].push(s)
  }
  return groups
})

const stats = computed(() => {
  const total = suggestions.value.length
  const resolved = suggestions.value.filter(s => s.is_resolved).length
  const high = suggestions.value.filter(s => s.priority === 'high' && !s.is_resolved).length
  return { total, resolved, pending: total - resolved, high }
})

async function fetchSuggestions() {
  if (!store.currentProject) return
  loading.value = true
  try {
    const { data } = await getSuggestions(store.currentProject.id)
    suggestions.value = data
  } catch {
    suggestions.value = []
  } finally {
    loading.value = false
  }
}

async function handleGenerate() {
  if (!store.currentProject) return
  generating.value = true
  try {
    const { data } = await generateSuggestions(store.currentProject.id)
    suggestions.value = [...data, ...suggestions.value]
  } catch (e: any) {
    alert(e?.response?.data?.detail || '生成失败，请确保已有审计报告')
  } finally {
    generating.value = false
  }
}

async function handleResolve(s: Suggestion) {
  try {
    await resolveSuggestion(s.id)
    s.is_resolved = true
  } catch { /* ignore */ }
}

async function handleDelete(id: number) {
  try {
    await deleteSuggestion(id)
    suggestions.value = suggestions.value.filter(s => s.id !== id)
  } catch { /* ignore */ }
}

onMounted(async () => {
  if (!store.currentProject) await store.fetchProjects()
  if (store.currentProject) fetchSuggestions()
})
</script>

<template>
  <div class="suggestions-page">
    <!-- Header -->
    <div class="header">
      <div>
        <h1>优化建议</h1>
        <div class="header-meta" v-if="stats.total > 0">
          共 {{ stats.total }} 条建议 · {{ stats.resolved }} 已完成 · {{ stats.high }} 高优先级
        </div>
      </div>
      <div class="header-actions">
        <div class="filter-row">
          <select v-model="filterCategory" class="filter-select">
            <option value="all">全部分类</option>
            <option v-for="(label, key) in categoryLabels" :key="key" :value="key">{{ label }}</option>
          </select>
          <div class="status-tabs">
            <button
              v-for="opt in (['all', 'pending', 'resolved'] as const)"
              :key="opt"
              class="tab"
              :class="{ active: filterStatus === opt }"
              @click="filterStatus = opt"
            >
              {{ opt === 'all' ? '全部' : opt === 'pending' ? '待处理' : '已完成' }}
            </button>
          </div>
        </div>
        <button
          class="btn btn-primary"
          :disabled="generating || !store.currentProject"
          @click="handleGenerate"
        >
          {{ generating ? 'AI分析中...' : '+ AI生成建议' }}
        </button>
      </div>
    </div>

    <div v-if="loading" class="loading-state">加载中...</div>

    <template v-else-if="suggestions.length > 0">
      <!-- Grouped suggestions -->
      <div v-for="(items, cat) in grouped" :key="cat" class="category-section">
        <div class="category-header">
          <span class="category-dot"></span>
          {{ categoryLabels[cat] || cat }}
          <span class="category-count">{{ items.length }}</span>
        </div>
        <div class="suggestion-list">
          <div
            v-for="s in items"
            :key="s.id"
            class="suggestion-card"
            :class="{ resolved: s.is_resolved }"
          >
            <div class="suggestion-header">
              <span class="priority-badge" :class="priorityLabels[s.priority]?.class">
                {{ priorityLabels[s.priority]?.label || s.priority }}
              </span>
              <span class="suggestion-title">{{ s.title }}</span>
              <span class="suggestion-time">{{ s.created_at?.slice(0, 10) }}</span>
            </div>
            <p class="suggestion-desc">{{ s.description }}</p>
            <div class="suggestion-actions">
              <button v-if="!s.is_resolved" class="btn-sm btn-resolve" @click="handleResolve(s)">
                标记完成
              </button>
              <span v-else class="resolved-tag">已完成</span>
              <button class="btn-sm btn-delete" @click="handleDelete(s.id)">删除</button>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- Empty -->
    <div v-else class="empty">
      <div class="empty-icon">💡</div>
      <h3>暂无优化建议</h3>
      <p>完成首次审计后，点击"AI生成建议"获取个性化优化方案</p>
    </div>
  </div>
</template>

<style scoped>
.suggestions-page {
  max-width: 1000px;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 24px;
  flex-wrap: wrap;
  gap: 16px;
}

.header h1 { font-size: 18px; font-weight: 600; }
.header-meta { font-size: 11px; color: var(--text-secondary); margin-top: 4px; }

.header-actions {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}

.filter-row {
  display: flex;
  gap: 8px;
  align-items: center;
}

.filter-select {
  padding: 5px 10px;
  border-radius: 4px;
  font-size: 11px;
  background: var(--bg-hover);
  color: var(--text-secondary);
  border: 1px solid var(--border-light);
}

.status-tabs {
  display: flex;
  gap: 4px;
}

.tab {
  padding: 5px 10px;
  border-radius: 4px;
  font-size: 11px;
  background: var(--bg-hover);
  color: var(--text-secondary);
  border: none;
  cursor: pointer;
  transition: all 0.15s;
}

.tab.active { background: var(--accent-dim); color: var(--accent); }

.btn {
  padding: 7px 14px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  cursor: pointer;
  border: none;
  font-weight: 500;
  transition: all 0.15s;
}

.btn-primary { background: var(--accent); color: var(--bg-base); }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }

/* Category Section */
.category-section { margin-bottom: 20px; }

.category-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 10px;
  color: var(--text-primary);
}

.category-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
}

.category-count {
  font-size: 10px;
  background: var(--bg-hover);
  padding: 1px 6px;
  border-radius: 10px;
  color: var(--text-muted);
}

.suggestion-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.suggestion-card {
  background: var(--bg-card);
  border-radius: var(--radius-md);
  padding: 14px 18px;
  border: 1px solid var(--border-light);
  transition: all 0.15s;
}

.suggestion-card:hover {
  border-color: var(--accent);
}

.suggestion-card.resolved {
  opacity: 0.6;
}

.suggestion-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.priority-badge {
  font-size: 9px;
  padding: 1px 6px;
  border-radius: 10px;
  font-weight: 600;
}

.priority-high { background: rgba(239,68,68,0.12); color: #ef4444; }
.priority-medium { background: rgba(245,158,11,0.12); color: #f59e0b; }
.priority-low { background: rgba(107,114,128,0.12); color: #6b7280; }

.suggestion-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  flex: 1;
}

.suggestion-time {
  font-size: 10px;
  color: var(--text-muted);
}

.suggestion-desc {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.5;
  margin: 0 0 8px 0;
}

.suggestion-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

.btn-sm {
  padding: 3px 10px;
  border-radius: 4px;
  font-size: 10px;
  cursor: pointer;
  border: none;
  font-weight: 500;
  transition: all 0.15s;
}

.btn-resolve { background: var(--accent-dim); color: var(--accent); }
.btn-delete { background: transparent; color: var(--text-muted); }
.btn-delete:hover { color: #ef4444; }

.resolved-tag {
  font-size: 10px;
  color: var(--status-good);
  font-weight: 500;
}

.loading-state { text-align: center; padding: 60px; color: var(--text-muted); font-size: 13px; }

.empty { text-align: center; padding: 60px 20px; color: var(--text-muted); }
.empty-icon { font-size: 48px; margin-bottom: 16px; }
.empty h3 { font-size: 16px; color: var(--text-primary); margin-bottom: 8px; }
.empty p { font-size: 13px; }

@media (max-width: 768px) {
  .header { flex-direction: column; }
  .filter-row { flex-wrap: wrap; }
}
</style>
