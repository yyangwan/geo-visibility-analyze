<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart, BarChart } from 'echarts/charts'
import {
  TitleComponent,
  TooltipComponent,
  GridComponent,
  LegendComponent,
  DataZoomComponent,
} from 'echarts/components'
import VChart from 'vue-echarts'
import { useProjectStore } from '../../stores/project'
import { PLATFORM_LABELS } from '../../constants/platforms'
import { ElMessage } from 'element-plus'
import {
  getTrendData,
  getAuditsHistory,
  type TrendPoint,
} from '../../api/client'
import LoadingSkeleton from '../../components/common/LoadingSkeleton.vue'
import ErrorState from '../../components/common/ErrorState.vue'
import EmptyState from '../../components/common/EmptyState.vue'

use([
  CanvasRenderer,
  LineChart,
  BarChart,
  TitleComponent,
  TooltipComponent,
  GridComponent,
  LegendComponent,
  DataZoomComponent,
])

const store = useProjectStore()
const trendData = ref<TrendPoint[]>([])
const loading = ref(false)
const error = ref('')
const period = ref<'daily' | 'weekly' | 'monthly'>('daily')
const limit = ref(30)

const platformLabels = PLATFORM_LABELS

// Quick range presets
const rangePresets = [
  { label: '7天', limit: 7 },
  { label: '30天', limit: 30 },
  { label: '90天', limit: 90 },
  { label: '180天', limit: 180 },
  { label: '365天', limit: 365 },
]

// Summary stats: current vs previous period
const summary = computed(() => {
  if (trendData.value.length < 2) return null
  const mid = Math.floor(trendData.value.length / 2)
  const recent = trendData.value.slice(mid)
  const previous = trendData.value.slice(0, mid)

  const avg = (arr: TrendPoint[], key: 'overall_score' | 'mention_rate') =>
    arr.length ? arr.reduce((s, d) => s + d[key], 0) / arr.length : 0

  const scoreNow = avg(recent, 'overall_score')
  const scorePrev = avg(previous, 'overall_score')
  const mentionNow = avg(recent, 'mention_rate')
  const mentionPrev = avg(previous, 'mention_rate')

  const latest = trendData.value[trendData.value.length - 1]
  const earliest = trendData.value[0]

  return {
    currentScore: Math.round(scoreNow),
    scoreChange: Math.round(scoreNow - scorePrev),
    currentMention: Math.round(mentionNow * 100),
    mentionChange: Math.round((mentionNow - mentionPrev) * 100),
    totalAudits: trendData.value.length,
    dateRange: `${earliest.date} ~ ${latest.date}`,
    latestScore: latest.overall_score,
    latestMention: Math.round(latest.mention_rate * 100),
  }
})

// Per-platform stats
const platformStats = computed(() => {
  const platforms = new Set<string>()
  trendData.value.forEach(d => {
    Object.keys(d.platform_scores || {}).forEach(p => platforms.add(p))
  })

  return Array.from(platforms).map(p => {
    const scores = trendData.value.map(d => d.platform_scores?.[p]).filter((v): v is number => v != null)
    const latest = scores.length ? scores[scores.length - 1] : 0
    const avg = scores.length ? scores.reduce((a, b) => a + b, 0) / scores.length : 0
    const best = scores.length ? Math.max(...scores) : 0
    const trend = scores.length >= 2 ? scores[scores.length - 1] - scores[0] : 0
    return {
      key: p,
      name: platformLabels[p] || p,
      latest: Math.round(latest),
      avg: Math.round(avg),
      best,
      trend,
      trendLabel: trend > 0 ? `+${Math.round(trend)}` : `${Math.round(trend)}`,
      trendClass: trend > 0 ? 'trend-up' : trend < 0 ? 'trend-down' : 'trend-flat',
    }
  }).sort((a, b) => b.latest - a.latest)
})

// Main trend chart option
const mainChartOption = computed(() => {
  if (trendData.value.length === 0) return {}

  const dates = trendData.value.map(d => d.date)
  const scores = trendData.value.map(d => d.overall_score)
  const mentionRates = trendData.value.map(d => Math.round(d.mention_rate * 100))

  // Extract all platform names
  const platformNames = new Set<string>()
  trendData.value.forEach(d => {
    Object.keys(d.platform_scores || {}).forEach(p => platformNames.add(p))
  })

  const platformSeries = Array.from(platformNames).map(platform => ({
    name: platformLabels[platform] || platform,
    type: 'line' as const,
    smooth: true,
    symbol: 'circle',
    symbolSize: 3,
    lineStyle: { width: 1.5 },
    data: trendData.value.map(d => d.platform_scores?.[platform] ?? null),
  }))

  return {
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#1a1d29',
      borderColor: '#27272a',
      textStyle: { color: '#e4e4e7', fontSize: 12 },
    },
    legend: {
      data: ['综合评分', '提及率(%)', ...Array.from(platformNames).map(p => platformLabels[p] || p)],
      textStyle: { color: '#71717a', fontSize: 10 },
      bottom: 0,
    },
    grid: {
      left: 50,
      right: 20,
      top: 20,
      bottom: 60,
    },
    dataZoom: trendData.value.length > 15 ? [
      {
        type: 'inside',
        start: Math.max(0, 100 - (15 / trendData.value.length) * 100),
        end: 100,
      },
    ] : undefined,
    xAxis: {
      type: 'category',
      data: dates,
      axisLine: { lineStyle: { color: '#27272a' } },
      axisLabel: { color: '#52525b', fontSize: 10, rotate: dates.length > 15 ? 45 : 0 },
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 100,
      axisLine: { show: false },
      splitLine: { lineStyle: { color: '#1e1e24' } },
      axisLabel: { color: '#52525b', fontSize: 10 },
    },
    series: [
      {
        name: '综合评分',
        type: 'line',
        smooth: true,
        symbol: 'circle',
        symbolSize: 6,
        lineStyle: { color: '#00d4aa', width: 2.5 },
        itemStyle: { color: '#00d4aa' },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(0,212,170,0.25)' },
              { offset: 1, color: 'rgba(0,212,170,0)' },
            ],
          },
        },
        data: scores,
      },
      {
        name: '提及率(%)',
        type: 'line',
        smooth: true,
        symbol: 'circle',
        symbolSize: 4,
        lineStyle: { color: '#4cc9f0', width: 1.5, type: 'dashed' },
        itemStyle: { color: '#4cc9f0' },
        data: mentionRates,
      },
      ...platformSeries,
    ],
  }
})

// Audit history
const auditsHistory = ref<Array<{
  id: number
  status: string
  platforms_json: string[]
  created_at: string
  completed_at: string | null
  error_message: string | null
}>>([])

async function fetchData() {
  const projectId = store.currentProject?.id
  if (!projectId) return

  loading.value = true
  error.value = ''
  try {
    const [trendRes, historyRes] = await Promise.all([
      getTrendData(projectId, period.value, limit.value),
      getAuditsHistory(projectId, 20),
    ])
    trendData.value = trendRes.data.data
    auditsHistory.value = historyRes.data
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '加载趋势数据失败'
    ElMessage.error(error.value)
  } finally {
    loading.value = false
  }
}

function setRange(l: number) {
  limit.value = l
}

watch(period, fetchData)
watch(limit, fetchData)
watch(() => store.currentProject, (p) => {
  if (p) fetchData()
})

onMounted(async () => {
  if (!store.currentProject) await store.fetchProjects()
  if (store.currentProject) fetchData()
})
</script>

<template>
  <div class="trends-page">
    <!-- Header -->
    <div class="header">
      <div>
        <h1>趋势追踪</h1>
        <div class="header-meta" v-if="summary">
          {{ summary.dateRange }} · 共 {{ summary.totalAudits }} 个数据点
        </div>
      </div>
      <div class="header-controls">
        <!-- Period tabs -->
        <div class="period-tabs">
          <button
            v-for="p in (['daily', 'weekly', 'monthly'] as const)"
            :key="p"
            class="tab"
            :class="{ active: period === p }"
            @click="period = p"
          >
            {{ p === 'daily' ? '按天' : p === 'weekly' ? '按周' : '按月' }}
          </button>
        </div>
        <!-- Range presets -->
        <div class="range-tabs">
          <button
            v-for="r in rangePresets"
            :key="r.limit"
            class="tab"
            :class="{ active: limit === r.limit }"
            @click="setRange(r.limit)"
          >
            {{ r.label }}
          </button>
        </div>
      </div>
    </div>

    <LoadingSkeleton v-if="loading" variant="chart" />
    <ErrorState v-else-if="error" :message="error" @retry="fetchData" />

    <template v-else-if="trendData.length > 0">
      <!-- Summary Cards -->
      <div v-if="summary" class="summary-row">
        <div class="summary-card">
          <div class="summary-label">最新评分</div>
          <div class="summary-value">{{ summary.latestScore }}</div>
          <div
            class="summary-change"
            :class="summary.scoreChange >= 0 ? 'change-up' : 'change-down'"
          >
            {{ summary.scoreChange >= 0 ? '+' : '' }}{{ summary.scoreChange }} vs 前半段
          </div>
        </div>
        <div class="summary-card">
          <div class="summary-label">最新提及率</div>
          <div class="summary-value">{{ summary.latestMention }}%</div>
          <div
            class="summary-change"
            :class="summary.mentionChange >= 0 ? 'change-up' : 'change-down'"
          >
            {{ summary.mentionChange >= 0 ? '+' : '' }}{{ summary.mentionChange }}% vs 前半段
          </div>
        </div>
        <div class="summary-card">
          <div class="summary-label">期间均分</div>
          <div class="summary-value">{{ summary.currentScore }}</div>
          <div class="summary-change change-flat">
            基于 {{ summary.totalAudits }} 次审计
          </div>
        </div>
        <div class="summary-card">
          <div class="summary-label">期间均提及率</div>
          <div class="summary-value">{{ summary.currentMention }}%</div>
          <div class="summary-change change-flat">
            {{ summary.dateRange }}
          </div>
        </div>
      </div>

      <!-- Main Trend Chart -->
      <div class="chart-card main-chart">
        <div class="section-title">可见性趋势</div>
        <VChart
          :option="mainChartOption"
          :autoresize="true"
          style="height: 380px"
        />
      </div>

      <!-- Two column: Platform breakdown + Audit history -->
      <div class="two-col">
        <!-- Platform Breakdown -->
        <div class="chart-card">
          <div class="section-title">平台趋势详情</div>
          <div v-if="platformStats.length > 0" class="table-scroll">
          <table class="platform-table">
            <thead>
              <tr>
                <th>平台</th>
                <th>最新评分</th>
                <th>平均分</th>
                <th>最高分</th>
                <th>变化趋势</th>
                <th>走势</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="p in platformStats" :key="p.key">
                <td class="platform-name">{{ p.name }}</td>
                <td>
                  <span class="score-badge" :class="p.latest >= 70 ? 'score-good' : p.latest >= 40 ? 'score-warn' : 'score-bad'">
                    {{ p.latest }}
                  </span>
                </td>
                <td>{{ p.avg }}</td>
                <td>{{ Math.round(p.best) }}</td>
                <td>
                  <span class="trend-badge" :class="p.trendClass">{{ p.trendLabel }}</span>
                </td>
                <td class="sparkline-cell">
                  <svg class="sparkline" viewBox="0 0 60 20" preserveAspectRatio="none">
                    <polyline
                      fill="none"
                      :stroke="p.trend >= 0 ? '#00d4aa' : '#ef4444'"
                      stroke-width="1.5"
                      :points="(() => {
                        const vals = trendData.map(d => d.platform_scores?.[p.key]).filter(v => v != null) as number[]
                        if (vals.length < 2) return ''
                        const mn = Math.min(...vals)
                        const mx = Math.max(...vals)
                        const range = mx - mn || 1
                        return vals.map((v, i) => `${(i / (vals.length - 1)) * 60},${20 - ((v - mn) / range) * 18}`).join(' ')
                      })()"
                    />
                  </svg>
                </td>
              </tr>
            </tbody>
          </table>
          </div>
          <div v-else class="no-data-sm">暂无平台数据</div>
        </div>

        <!-- Audit History -->
        <div class="chart-card">
          <div class="section-title">审计历史</div>
          <div v-if="auditsHistory.length > 0" class="audit-list">
            <div
              v-for="audit in auditsHistory"
              :key="audit.id"
              class="audit-item"
            >
              <div class="audit-dot" :class="'status-' + audit.status"></div>
              <div class="audit-info">
                <div class="audit-title">
                  审计 #{{ audit.id }}
                  <span class="audit-status" :class="'status-' + audit.status">
                    {{ audit.status === 'completed' ? '完成' : audit.status === 'running' ? '进行中' : audit.status === 'failed' ? '失败' : audit.status === 'partial' ? '部分完成' : '等待中' }}
                  </span>
                </div>
                <div class="audit-meta">
                  {{ audit.platforms_json?.map(p => platformLabels[p] || p).join(', ') || '-' }}
                </div>
                <div class="audit-time">
                  {{ audit.created_at?.slice(0, 16).replace('T', ' ') }}
                  <span v-if="audit.completed_at">
                    → {{ audit.completed_at?.slice(11, 16) }}
                  </span>
                </div>
              </div>
            </div>
          </div>
          <div v-else class="no-data-sm">暂无审计记录</div>
        </div>
      </div>
    </template>

    <!-- Empty State -->
    <EmptyState
      v-else
      icon="📈"
      title="暂无趋势数据"
      description="完成多次审计后，此处将展示可见性变化趋势"
    />
  </div>
</template>

<style scoped>
.trends-page {
  max-width: 1200px;
}

/* Header */
.header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 24px;
  gap: 16px;
  flex-wrap: wrap;
}

.header h1 {
  font-size: 18px;
  font-weight: 600;
}

.header-meta {
  font-size: 11px;
  color: var(--text-secondary);
  margin-top: 4px;
}

.header-controls {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}

.period-tabs,
.range-tabs {
  display: flex;
  gap: 4px;
}

.tab {
  padding: 5px 12px;
  border-radius: 4px;
  font-size: 11px;
  background: var(--bg-hover);
  color: var(--text-secondary);
  border: none;
  cursor: pointer;
  transition: all 0.15s;
}

.tab.active {
  background: var(--accent-dim);
  color: var(--accent);
}

/* Summary Cards */
.summary-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
  margin-bottom: 20px;
}

.summary-card {
  background: var(--bg-card);
  border-radius: var(--radius-md);
  padding: 16px 18px;
  border: 1px solid var(--border-light);
}

.summary-label {
  font-size: 11px;
  color: var(--text-muted);
  margin-bottom: 8px;
}

.summary-value {
  font-size: 28px;
  font-weight: 700;
  color: var(--text-primary);
  line-height: 1;
}

.summary-change {
  font-size: 11px;
  margin-top: 6px;
}

.change-up { color: var(--status-good); }
.change-down { color: var(--status-bad); }
.change-flat { color: var(--text-muted); }

/* Chart Card */
.chart-card {
  background: var(--bg-card);
  border-radius: var(--radius-md);
  padding: 18px;
  border: 1px solid var(--border-light);
}

.main-chart {
  margin-bottom: 16px;
}

.section-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 14px;
}

/* Two Column */
.two-col {
  display: grid;
  grid-template-columns: 1.2fr 1fr;
  gap: 16px;
}

/* Platform Table */
.platform-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.platform-table th {
  text-align: left;
  padding: 8px 8px;
  color: var(--text-muted);
  font-weight: 500;
  border-bottom: 1px solid var(--border);
  font-size: 10px;
  text-transform: uppercase;
}

.platform-table td {
  padding: 10px 8px;
  border-bottom: 1px solid var(--border-light);
}

.platform-table tr:hover td {
  background: var(--bg-hover);
}

.platform-name {
  font-weight: 500;
  color: var(--text-primary);
}

.score-badge {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 600;
}

.score-good { background: rgba(0,212,170,0.12); color: var(--status-good); }
.score-warn { background: rgba(250,204,21,0.12); color: var(--status-warn); }
.score-bad { background: rgba(239,68,68,0.12); color: var(--status-bad); }

.trend-badge {
  font-size: 11px;
  font-weight: 600;
}

.trend-up { color: var(--status-good); }
.trend-down { color: var(--status-bad); }
.trend-flat { color: var(--text-muted); }

.sparkline-cell {
  width: 70px;
}

.sparkline {
  width: 60px;
  height: 20px;
  display: block;
}

/* Audit History */
.audit-list {
  display: flex;
  flex-direction: column;
  gap: 0;
  max-height: 400px;
  overflow-y: auto;
}

.audit-item {
  display: flex;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid var(--border-light);
}

.audit-item:last-child {
  border-bottom: none;
}

.audit-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-top: 5px;
  flex-shrink: 0;
}

.audit-dot.status-completed { background: var(--status-good); }
.audit-dot.status-running { background: var(--status-warn); }
.audit-dot.status-failed { background: var(--status-bad); }
.audit-dot.status-pending { background: var(--text-muted); }
.audit-dot.status-partial { background: #f59e0b; }

.audit-info {
  flex: 1;
  min-width: 0;
}

.audit-title {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-primary);
  display: flex;
  align-items: center;
  gap: 6px;
}

.audit-status {
  font-size: 9px;
  padding: 1px 6px;
  border-radius: 10px;
  font-weight: 600;
}

.audit-status.status-completed { background: rgba(0,212,170,0.12); color: var(--status-good); }
.audit-status.status-running { background: rgba(250,204,21,0.12); color: var(--status-warn); }
.audit-status.status-failed { background: rgba(239,68,68,0.12); color: var(--status-bad); }
.audit-status.status-pending { background: rgba(113,113,122,0.12); color: var(--text-muted); }
.audit-status.status-partial { background: rgba(245,158,11,0.12); color: #f59e0b; }

.audit-meta {
  font-size: 11px;
  color: var(--text-secondary);
  margin-top: 2px;
}

.audit-time {
  font-size: 10px;
  color: var(--text-muted);
  margin-top: 2px;
}

.no-data-sm {
  text-align: center;
  padding: 24px;
  color: var(--text-muted);
  font-size: 12px;
}

@media (max-width: 768px) {
  .summary-row {
    grid-template-columns: repeat(2, 1fr);
  }
  .two-col {
    grid-template-columns: 1fr;
  }
  .header {
    flex-direction: column;
  }
}
</style>
