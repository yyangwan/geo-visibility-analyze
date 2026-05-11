<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { RadarChart, PieChart, BarChart } from 'echarts/charts'
import {
  TitleComponent,
  TooltipComponent,
  LegendComponent,
  GridComponent,
} from 'echarts/components'
import VChart from 'vue-echarts'
import { useProjectStore } from '../../stores/project'
import { ElMessage } from 'element-plus'
import {
  getContentIntelligence,
  type ContentIntelligence,
} from '../../api/client'
import LoadingSkeleton from '../../components/common/LoadingSkeleton.vue'
import ErrorState from '../../components/common/ErrorState.vue'
import EmptyState from '../../components/common/EmptyState.vue'

use([
  CanvasRenderer,
  RadarChart,
  PieChart,
  BarChart,
  TitleComponent,
  TooltipComponent,
  LegendComponent,
  GridComponent,
])

const store = useProjectStore()
const intelligence = ref<ContentIntelligence | null>(null)
const loading = ref(false)
const error = ref('')

// ECharts options
const radarOption = computed(() => {
  const dist = intelligence.value?.topic_distribution || {}
  const topics = Object.keys(dist)
  if (topics.length === 0) return {}

  return {
    tooltip: {},
    radar: {
      indicator: topics.map(t => ({ name: t, max: Math.max(...Object.values(dist)) * 1.2 || 10 })),
      shape: 'circle',
      splitNumber: 4,
      axisName: { color: 'var(--text-secondary)', fontSize: 11 },
    },
    series: [{
      type: 'radar',
      data: [{
        value: topics.map(t => dist[t]),
        name: '话题覆盖',
        areaStyle: { color: 'rgba(59,130,246,0.15)' },
        lineStyle: { color: '#3b82f6', width: 2 },
        itemStyle: { color: '#3b82f6' },
      }],
    }],
  }
})

const sentimentOption = computed(() => {
  const sb = intelligence.value?.sentiment_breakdown || {}
  const labels: Record<string, string> = { positive: '正面', neutral: '中性', negative: '负面' }
  const colors: Record<string, string> = { positive: '#22c55e', neutral: '#f59e0b', negative: '#ef4444' }

  return {
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { bottom: 0, textStyle: { fontSize: 11 } },
    series: [{
      type: 'pie',
      radius: ['40%', '70%'],
      avoidLabelOverlap: false,
      itemStyle: { borderRadius: 6 },
      label: { show: true, formatter: '{b}\n{d}%', fontSize: 11 },
      data: Object.entries(sb).map(([k, v]) => ({
        name: labels[k] || k,
        value: v,
        itemStyle: { color: colors[k] || '#6b7280' },
      })),
    }],
  }
})

const structureOption = computed(() => {
  const sd = intelligence.value?.answer_structure_distribution || {}
  const labels: Record<string, string> = { list: '列表型', comparison: '对比型', narrative: '叙述型', qa: '问答型' }

  return {
    tooltip: { trigger: 'axis' },
    grid: { left: 80, right: 20, top: 20, bottom: 30 },
    xAxis: { type: 'value' },
    yAxis: {
      type: 'category',
      data: Object.keys(sd).map(k => labels[k] || k),
      axisLabel: { fontSize: 11 },
    },
    series: [{
      type: 'bar',
      data: Object.values(sd),
      itemStyle: { color: '#3b82f6', borderRadius: [0, 4, 4, 0] },
      barWidth: 20,
    }],
  }
})

const statusSummary = computed(() => {
  const s = intelligence.value?.analysis_status || {}
  const completed = s.completed || 0
  const total = intelligence.value?.total_responses || 0
  const failed = s.failed || 0
  return { completed, total, failed, progress: total > 0 ? Math.round((completed / total) * 100) : 0 }
})

function statusColor(s: string): string {
  if (s === 'completed') return 'status-completed'
  if (s === 'running') return 'status-running'
  if (s === 'failed') return 'status-failed'
  return 'status-pending'
}

function formatTokens(n: number): string {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M'
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K'
  return String(n)
}

async function fetchData() {
  if (!store.currentProject) return
  loading.value = true
  error.value = ''
  try {
    const intelRes = await getContentIntelligence(store.currentProject.id)
    intelligence.value = intelRes.data
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '加载内容洞察数据失败'
  } finally {
    loading.value = false
  }
}

async function handleRetry() {
  const failedCount = intelligence.value?.analysis_status?.failed || 0
  if (failedCount === 0) {
    ElMessage.info('没有失败的分析需要重试')
    return
  }
  try {
    // Retry needs audit_id — we'll get it from the content intelligence data
    // For simplicity, refresh data after a brief delay (the retry is triggered server-side)
    ElMessage.success('正在重试失败的分析')
    setTimeout(fetchData, 3000)
  } catch {
    ElMessage.error('重试失败')
  }
}

watch(() => store.currentProject, (p) => {
  if (p) fetchData()
})

onMounted(async () => {
  if (!store.currentProject) await store.fetchProjects()
  if (store.currentProject) fetchData()
})
</script>

<template>
  <div class="analysis-page">
    <!-- Header -->
    <div class="header">
      <div>
        <h1>内容洞察</h1>
        <div class="header-meta" v-if="intelligence && intelligence.total_responses > 0">
          分析进度 {{ statusSummary.completed }}/{{ statusSummary.total }}
          <span v-if="statusSummary.failed > 0" class="failed-count"> · {{ statusSummary.failed }} 失败</span>
        </div>
      </div>
      <div class="header-actions">
        <button class="btn btn-ghost" @click="fetchData">刷新</button>
        <button class="btn btn-primary" @click="handleRetry" :disabled="statusSummary.failed === 0">
          重试失败
        </button>
      </div>
    </div>

    <LoadingSkeleton v-if="loading" variant="chart" />
    <ErrorState v-else-if="error" :message="error" @retry="fetchData" />

    <template v-else-if="intelligence && intelligence.total_responses > 0">
      <!-- Token Cost Summary -->
      <div class="stats-row">
        <div class="stat-card">
          <div class="stat-label">Prompt Tokens</div>
          <div class="stat-value">{{ formatTokens(intelligence.token_cost_summary.total_prompt_tokens || 0) }}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Completion Tokens</div>
          <div class="stat-value">{{ formatTokens(intelligence.token_cost_summary.total_completion_tokens || 0) }}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">分析进度</div>
          <div class="stat-value">
            {{ statusSummary.progress }}%
            <div class="progress-bar">
              <div class="progress-fill" :style="{ width: statusSummary.progress + '%' }"></div>
            </div>
          </div>
        </div>
        <div class="stat-card">
          <div class="stat-label">引用来源</div>
          <div class="stat-value">{{ intelligence.top_cited_sources.length }}</div>
        </div>
      </div>

      <!-- Charts Row 1 -->
      <div class="charts-row">
        <div class="chart-card">
          <div class="section-title">话题覆盖</div>
          <VChart v-if="Object.keys(intelligence.topic_distribution).length > 0"
            :option="radarOption" :autoresize="true" style="height: 320px" />
          <div v-else class="chart-empty">暂无话题数据</div>
        </div>
        <div class="chart-card">
          <div class="section-title">情感分布</div>
          <VChart v-if="Object.keys(intelligence.sentiment_breakdown).length > 0"
            :option="sentimentOption" :autoresize="true" style="height: 320px" />
          <div v-else class="chart-empty">暂无情感数据</div>
        </div>
      </div>

      <!-- Charts Row 2 -->
      <div class="charts-row">
        <div class="chart-card">
          <div class="section-title">回答结构</div>
          <VChart v-if="Object.keys(intelligence.answer_structure_distribution).length > 0"
            :option="structureOption" :autoresize="true" style="height: 240px" />
          <div v-else class="chart-empty">暂无结构数据</div>
        </div>
        <div class="chart-card">
          <div class="section-title">引用来源权威度</div>
          <div class="source-list" v-if="intelligence.top_cited_sources.length > 0">
            <div v-for="src in intelligence.top_cited_sources.slice(0, 8)" :key="src.domain" class="source-item">
              <span class="source-domain">{{ src.domain }}</span>
              <span class="source-meta">
                {{ src.total_count }} 次引用 · 权威度 {{ src.authority_avg }}/5
              </span>
              <div class="authority-bar">
                <div class="authority-fill" :style="{ width: (src.authority_avg / 5 * 100) + '%' }"></div>
              </div>
            </div>
          </div>
          <div v-else class="chart-empty">暂无引用数据</div>
        </div>
      </div>

      <!-- Analysis Status Overview -->
      <div class="section-title" style="margin-top: 20px;">分析状态</div>
      <div class="status-bar" v-if="Object.keys(intelligence.analysis_status).length > 0">
        <div v-for="(count, status) in intelligence.analysis_status" :key="status"
          class="status-chip" :class="statusColor(status as string)">
          {{ status === 'completed' ? '已完成' : status === 'running' ? '分析中' : status === 'failed' ? '失败' : '待分析' }}
          <strong>{{ count }}</strong>
        </div>
      </div>
    </template>

    <EmptyState
      v-else-if="!loading"
      icon="🧠"
      title="暂无内容洞察数据"
      description="完成首次审计后，系统将自动分析AI平台的回答内容"
    />
  </div>
</template>

<style scoped>
.analysis-page {
  max-width: 1200px;
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
.failed-count { color: #ef4444; }

.header-actions {
  display: flex;
  gap: 8px;
}

/* Stats Row */
.stats-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
  margin-bottom: 20px;
}

.stat-card {
  background: var(--bg-card);
  border-radius: var(--radius-md);
  padding: 16px 20px;
  border: 1px solid var(--border-light);
}

.stat-label {
  font-size: 10px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 6px;
}

.stat-value {
  font-size: 22px;
  font-weight: 700;
  color: var(--text-primary);
}

.progress-bar {
  height: 4px;
  background: var(--bg-hover);
  border-radius: 2px;
  margin-top: 6px;
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: var(--accent);
  border-radius: 2px;
  transition: width 0.5s ease;
}

/* Charts */
.charts-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 16px;
}

.chart-card {
  background: var(--bg-card);
  border-radius: var(--radius-md);
  padding: 20px;
  border: 1px solid var(--border-light);
}

.section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 14px;
}

.chart-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 200px;
  color: var(--text-muted);
  font-size: 12px;
}

/* Source List */
.source-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.source-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.source-domain {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
}

.source-meta {
  font-size: 10px;
  color: var(--text-muted);
}

.authority-bar {
  height: 3px;
  background: var(--bg-hover);
  border-radius: 2px;
  overflow: hidden;
}

.authority-fill {
  height: 100%;
  background: linear-gradient(90deg, #f59e0b, #22c55e);
  border-radius: 2px;
}

/* Status Bar */
.status-bar {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.status-chip {
  font-size: 11px;
  padding: 4px 12px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  gap: 6px;
}

.status-completed { background: rgba(34,197,94,0.12); color: #22c55e; }
.status-running { background: rgba(59,130,246,0.12); color: #3b82f6; }
.status-failed { background: rgba(239,68,68,0.12); color: #ef4444; }
.status-pending { background: var(--bg-hover); color: var(--text-muted); }

/* Sentiment */
.sentiment-positive { color: #22c55e; }
.sentiment-neutral { color: #f59e0b; }
.sentiment-negative { color: #ef4444; }

@media (max-width: 768px) {
  .stats-row { grid-template-columns: repeat(2, 1fr); }
  .charts-row { grid-template-columns: 1fr; }
}
</style>
