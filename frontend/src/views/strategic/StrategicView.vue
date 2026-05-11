<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart, BarChart, ScatterChart, HeatmapChart } from 'echarts/charts'
import {
  TitleComponent,
  TooltipComponent,
  GridComponent,
  LegendComponent,
  VisualMapComponent,
  MarkAreaComponent,
} from 'echarts/components'
import VChart from 'vue-echarts'
import { useProjectStore } from '../../stores/project'
import { PLATFORM_LABELS } from '../../constants/platforms'
import { ElMessage } from 'element-plus'
import {
  getSourceAuthorityTrends,
  getCompetitorPositioning,
  getStructureEvolution,
  compareAudits,
  getAuditsHistory,
  type SourceAuthorityTrends,
  type CompetitorPositioning,
  type AnswerStructureEvolution,
  type MultiAuditComparison,
} from '../../api/client'
import LoadingSkeleton from '../../components/common/LoadingSkeleton.vue'
import ErrorState from '../../components/common/ErrorState.vue'
import EmptyState from '../../components/common/EmptyState.vue'

use([
  CanvasRenderer,
  LineChart,
  BarChart,
  ScatterChart,
  HeatmapChart,
  TitleComponent,
  TooltipComponent,
  GridComponent,
  LegendComponent,
  VisualMapComponent,
  MarkAreaComponent,
])

const store = useProjectStore()
const activeTab = ref<'source' | 'positioning' | 'structure' | 'compare'>('source')
const loading = ref(false)
const error = ref('')

const platformLabels = PLATFORM_LABELS

// Tab 1: Source Authority Trends
const sourceData = ref<SourceAuthorityTrends>({ audits: [], domain_trends: [], platform_preferences: [], authority_trend: {} })

const sourceTrendChartOption = computed(() => {
  if (sourceData.value.domain_trends.length === 0) return {}
  const dates = sourceData.value.audits.map(a => a.date)
  const domains = sourceData.value.domain_trends.slice(0, 8)
  const colors = ['#00d4aa', '#4cc9f0', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#06b6d4', '#84cc16']
  return {
    tooltip: { trigger: 'axis', backgroundColor: '#1a1d29', borderColor: '#27272a', textStyle: { color: '#e4e4e7', fontSize: 12 } },
    legend: { data: domains.map(d => d.domain), textStyle: { color: '#71717a', fontSize: 10 }, bottom: 0, type: 'scroll' },
    grid: { left: 50, right: 20, top: 20, bottom: 60 },
    xAxis: { type: 'category', data: dates, axisLine: { lineStyle: { color: '#27272a' } }, axisLabel: { color: '#52525b', fontSize: 10 } },
    yAxis: { type: 'value', axisLine: { show: false }, splitLine: { lineStyle: { color: '#1e1e24' } }, axisLabel: { color: '#52525b', fontSize: 10 } },
    series: domains.map((d, i) => ({
      name: d.domain,
      type: 'line' as const,
      smooth: true,
      symbol: 'circle',
      symbolSize: 5,
      lineStyle: { color: colors[i % colors.length], width: 2 },
      itemStyle: { color: colors[i % colors.length] },
      data: d.data.map(pt => pt.count),
    })),
  }
})

const platformSourceChartOption = computed(() => {
  const prefs = sourceData.value.platform_preferences
  if (prefs.length === 0) return {}
  const platforms = prefs.map(p => platformLabels[p.platform] || p.platform)
  const allDomains = new Set<string>()
  prefs.forEach(p => p.top_domains.forEach(d => allDomains.add(d.domain)))
  const domainList = Array.from(allDomains).slice(0, 6)
  const colors = ['#00d4aa', '#4cc9f0', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899']
  return {
    tooltip: { trigger: 'axis', backgroundColor: '#1a1d29', borderColor: '#27272a', textStyle: { color: '#e4e4e7', fontSize: 12 } },
    legend: { data: domainList, textStyle: { color: '#71717a', fontSize: 10 }, bottom: 0 },
    grid: { left: 60, right: 20, top: 20, bottom: 60 },
    xAxis: { type: 'category', data: platforms, axisLine: { lineStyle: { color: '#27272a' } }, axisLabel: { color: '#52525b', fontSize: 10 } },
    yAxis: { type: 'value', axisLine: { show: false }, splitLine: { lineStyle: { color: '#1e1e24' } }, axisLabel: { color: '#52525b', fontSize: 10 } },
    series: domainList.map((domain, i) => ({
      name: domain,
      type: 'bar' as const,
      stack: 'total',
      emphasis: { focus: 'series' as const },
      itemStyle: { color: colors[i % colors.length] },
      data: prefs.map(p => p.top_domains.find(d => d.domain === domain)?.count || 0),
    })),
  }
})

// Tab 2: Competitor Positioning
const positioningData = ref<CompetitorPositioning>({ brands: [], quadrant_labels: {} })

const positioningChartOption = computed(() => {
  if (positioningData.value.brands.length === 0) return {}
  const brands = positioningData.value.brands
  return {
    tooltip: {
      backgroundColor: '#1a1d29',
      borderColor: '#27272a',
      textStyle: { color: '#e4e4e7', fontSize: 12 },
      formatter: (params: any) => {
        const d = params.data
        return `<b>${d[3]}</b><br/>提及频率: ${(d[1] * 100).toFixed(1)}%<br/>正面情感: ${(d[0] * 100).toFixed(1)}%<br/>平均权威度: ${d[2].toFixed(1)}`
      },
    },
    grid: { left: 60, right: 30, top: 30, bottom: 50 },
    xAxis: {
      name: '正面情感率',
      nameTextStyle: { color: '#71717a', fontSize: 10 },
      min: 0, max: 1,
      axisLine: { lineStyle: { color: '#27272a' } },
      axisLabel: { color: '#52525b', fontSize: 10, formatter: (v: number) => `${Math.round(v * 100)}%` },
      splitLine: { lineStyle: { color: '#1e1e24' } },
    },
    yAxis: {
      name: '提及频率',
      nameTextStyle: { color: '#71717a', fontSize: 10 },
      min: 0, max: 1,
      axisLine: { lineStyle: { color: '#27272a' } },
      axisLabel: { color: '#52525b', fontSize: 10, formatter: (v: number) => `${Math.round(v * 100)}%` },
      splitLine: { lineStyle: { color: '#1e1e24' } },
    },
    series: [{
      type: 'scatter' as const,
      symbolSize: (data: number[]) => Math.max(data[2] * 15, 20),
      data: brands.map(b => [b.sentiment_positive_rate, b.mention_frequency, b.avg_authority || 2.5, b.name]),
      itemStyle: {
        color: (params: any) => {
          const brand = brands[params.dataIndex]
          return brand.is_competitor ? '#4cc9f0' : '#00d4aa'
        },
        shadowBlur: 10,
        shadowColor: 'rgba(0,0,0,0.3)',
      },
      label: { show: true, formatter: (params: any) => params.data[3], position: 'top' as const, color: '#e4e4e7', fontSize: 11 },
      markArea: {
        silent: true,
        data: [
          [{ xAxis: '50%', yAxis: '50%', itemStyle: { color: 'rgba(0,212,170,0.04)' } }, { xAxis: '100%', yAxis: '100%' }],
          [{ xAxis: '0%', yAxis: '50%', itemStyle: { color: 'rgba(76,201,240,0.04)' } }, { xAxis: '50%', yAxis: '100%' }],
          [{ xAxis: '0%', yAxis: '0%', itemStyle: { color: 'rgba(239,68,68,0.04)' } }, { xAxis: '50%', yAxis: '50%' }],
          [{ xAxis: '50%', yAxis: '0%', itemStyle: { color: 'rgba(245,158,11,0.04)' } }, { xAxis: '100%', yAxis: '50%' }],
        ],
      },
      markLine: {
        silent: true,
        lineStyle: { color: '#27272a', type: 'dashed' as const },
        data: [
          { xAxis: 0.5, label: { show: false } },
          { yAxis: 0.5, label: { show: false } },
        ],
      },
    }],
  }
})

const trajectoryChartOption = computed(() => {
  const brands = positioningData.value.brands
  if (brands.length === 0 || brands[0].trajectory.length === 0) return {}
  const dates = brands[0].trajectory.map(t => t.date)
  const colors = ['#00d4aa', '#4cc9f0', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#06b6d4', '#84cc16']
  return {
    tooltip: { trigger: 'axis', backgroundColor: '#1a1d29', borderColor: '#27272a', textStyle: { color: '#e4e4e7', fontSize: 12 } },
    legend: { data: brands.map(b => b.name), textStyle: { color: '#71717a', fontSize: 10 }, bottom: 0, type: 'scroll' },
    grid: { left: 50, right: 20, top: 20, bottom: 60 },
    xAxis: { type: 'category', data: dates, axisLine: { lineStyle: { color: '#27272a' } }, axisLabel: { color: '#52525b', fontSize: 10 } },
    yAxis: { type: 'value', axisLine: { show: false }, splitLine: { lineStyle: { color: '#1e1e24' } }, axisLabel: { color: '#52525b', fontSize: 10, formatter: (v: number) => `${Math.round(v * 100)}%` } },
    series: brands.map((b, i) => ({
      name: b.name,
      type: 'line' as const,
      smooth: true,
      symbol: 'circle',
      symbolSize: 5,
      lineStyle: { color: b.is_competitor ? colors[(i + 1) % colors.length] : colors[0], width: b.is_competitor ? 1.5 : 2.5 },
      itemStyle: { color: b.is_competitor ? colors[(i + 1) % colors.length] : colors[0] },
      data: b.trajectory.map(t => t.mention_rate),
    })),
  }
})

// Tab 3: Answer Structure Evolution
const structureData = ref<AnswerStructureEvolution>({
  audits: [],
  structure_distribution: {},
  platform_structure: {},
  correlation: {},
  transitions: [],
})

const structureChartOption = computed(() => {
  const audits = structureData.value.audits
  if (audits.length === 0) return {}
  const dates = audits.map(a => a.date)
  const types = Object.keys(structureData.value.structure_distribution)
  const colors: Record<string, string> = { list: '#00d4aa', comparison: '#4cc9f0', narrative: '#f59e0b', qa: '#8b5cf6', unknown: '#71717a' }
  return {
    tooltip: { trigger: 'axis', backgroundColor: '#1a1d29', borderColor: '#27272a', textStyle: { color: '#e4e4e7', fontSize: 12 } },
    legend: { data: types, textStyle: { color: '#71717a', fontSize: 10 }, bottom: 0 },
    grid: { left: 50, right: 20, top: 20, bottom: 60 },
    xAxis: { type: 'category', data: dates, axisLine: { lineStyle: { color: '#27272a' } }, axisLabel: { color: '#52525b', fontSize: 10 } },
    yAxis: { type: 'value', axisLine: { show: false }, splitLine: { lineStyle: { color: '#1e1e24' } }, axisLabel: { color: '#52525b', fontSize: 10 } },
    series: types.map(t => ({
      name: t,
      type: 'bar' as const,
      stack: 'total',
      emphasis: { focus: 'series' as const },
      itemStyle: { color: colors[t] || '#71717a' },
      data: structureData.value.structure_distribution[t].map(p => p.count),
    })),
  }
})

const platformHeatmapOption = computed(() => {
  const ps = structureData.value.platform_structure
  const platforms = Object.keys(ps)
  if (platforms.length === 0) return {}
  const types = new Set<string>()
  platforms.forEach(p => Object.keys(ps[p]).forEach(t => types.add(t)))
  const typeList = Array.from(types)
  const data: number[][] = []
  platforms.forEach((p, pi) => {
    typeList.forEach((t, ti) => {
      data.push([ti, pi, ps[p][t] || 0])
    })
  })
  return {
    tooltip: {
      backgroundColor: '#1a1d29',
      borderColor: '#27272a',
      textStyle: { color: '#e4e4e7', fontSize: 12 },
      formatter: (params: any) => `${platformLabels[platforms[params.data[1]]] || platforms[params.data[1]]} · ${typeList[params.data[0]]}: ${params.data[2]}次`,
    },
    grid: { left: 80, right: 40, top: 10, bottom: 40 },
    xAxis: { type: 'category', data: typeList, axisLine: { lineStyle: { color: '#27272a' } }, axisLabel: { color: '#52525b', fontSize: 10 } },
    yAxis: { type: 'category', data: platforms.map(p => platformLabels[p] || p), axisLine: { lineStyle: { color: '#27272a' } }, axisLabel: { color: '#52525b', fontSize: 10 } },
    visualMap: { min: 0, max: 20, show: false, inRange: { color: ['#1a1d29', '#00d4aa'] } },
    series: [{
      type: 'heatmap' as const,
      data,
      label: { show: true, color: '#e4e4e7', fontSize: 10 },
      itemStyle: { borderColor: '#1a1d29', borderWidth: 2 },
    }],
  }
})

// Tab 4: Multi-Audit Comparison
const compareData = ref<MultiAuditComparison>({
  audits: [],
  diffs: { mention_rate_delta: 0, score_delta: 0, source_changes: { added: [], removed: [] }, competitor_changes: [] },
})
const auditsHistory = ref<Array<{ id: number; status: string; created_at: string }>>([])
const selectedAuditIds = ref<number[]>([])

const compareDeltaChartOption = computed(() => {
  const changes = compareData.value.diffs?.competitor_changes || []
  if (changes.length === 0) return {}
  return {
    tooltip: { backgroundColor: '#1a1d29', borderColor: '#27272a', textStyle: { color: '#e4e4e7', fontSize: 12 } },
    grid: { left: 80, right: 20, top: 10, bottom: 20 },
    xAxis: { type: 'value', axisLine: { show: false }, splitLine: { lineStyle: { color: '#1e1e24' } }, axisLabel: { color: '#52525b', fontSize: 10, formatter: (v: number) => `${Math.round(v * 100)}%` } },
    yAxis: { type: 'category', data: changes.map(c => c.brand), axisLine: { lineStyle: { color: '#27272a' } }, axisLabel: { color: '#52525b', fontSize: 10 } },
    series: [{
      type: 'bar' as const,
      data: changes.map(c => ({
        value: c.delta,
        itemStyle: { color: c.delta >= 0 ? '#00d4aa' : '#ef4444' },
      })),
    }],
  }
})

// Data fetching
async function fetchSourceData() {
  const projectId = store.currentProject?.id
  if (!projectId) return
  try {
    const res = await getSourceAuthorityTrends(projectId, 10)
    sourceData.value = res.data
  } catch (e: any) {
    ElMessage.error('加载来源权威趋势失败')
  }
}

async function fetchPositioningData() {
  const projectId = store.currentProject?.id
  if (!projectId) return
  try {
    const res = await getCompetitorPositioning(projectId)
    positioningData.value = res.data
  } catch (e: any) {
    ElMessage.error('加载竞品定位数据失败')
  }
}

async function fetchStructureData() {
  const projectId = store.currentProject?.id
  if (!projectId) return
  try {
    const res = await getStructureEvolution(projectId, 10)
    structureData.value = res.data
  } catch (e: any) {
    ElMessage.error('加载回答结构数据失败')
  }
}

async function fetchCompareData() {
  const projectId = store.currentProject?.id
  if (!projectId) return
  try {
    const historyRes = await getAuditsHistory(projectId, 20)
    auditsHistory.value = historyRes.data
  } catch (e: any) {
    ElMessage.error('加载审计历史失败')
  }
}

async function runComparison() {
  const projectId = store.currentProject?.id
  if (!projectId || selectedAuditIds.value.length < 2) return
  try {
    const res = await compareAudits(projectId, selectedAuditIds.value)
    compareData.value = res.data
  } catch (e: any) {
    ElMessage.error('加载对比数据失败')
  }
}

async function loadTab() {
  loading.value = true
  error.value = ''
  try {
    switch (activeTab.value) {
      case 'source': await fetchSourceData(); break
      case 'positioning': await fetchPositioningData(); break
      case 'structure': await fetchStructureData(); break
      case 'compare': await fetchCompareData(); break
    }
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '加载数据失败'
  } finally {
    loading.value = false
  }
}

watch(activeTab, loadTab)
watch(() => store.currentProject, (p) => {
  if (p) loadTab()
})

onMounted(async () => {
  if (!store.currentProject) await store.fetchProjects()
  if (store.currentProject) loadTab()
})

const tabs = [
  { key: 'source' as const, label: '来源权威趋势', icon: '🌐' },
  { key: 'positioning' as const, label: '竞品定位地图', icon: '🗺️' },
  { key: 'structure' as const, label: '回答结构演变', icon: '📋' },
  { key: 'compare' as const, label: '多审计对比', icon: '⚖️' },
]

const structureTypeLabels: Record<string, string> = {
  list: '列表型', comparison: '对比型', narrative: '叙述型', qa: '问答型', unknown: '未知',
}
</script>

<template>
  <div class="strategic-page">
    <!-- Header -->
    <div class="header">
      <div>
        <h1>战略智能</h1>
        <div class="header-meta">跨审计趋势分析与品牌定位追踪</div>
      </div>
    </div>

    <!-- Tab Bar -->
    <div class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab-btn"
        :class="{ active: activeTab === tab.key }"
        @click="activeTab = tab.key"
      >
        <span class="tab-icon" aria-hidden="true">{{ tab.icon }}</span>
        {{ tab.label }}
      </button>
    </div>

    <LoadingSkeleton v-if="loading" variant="chart" />
    <ErrorState v-else-if="error" :message="error" @retry="loadTab" />

    <!-- Tab 1: Source Authority Trends -->
    <template v-else-if="activeTab === 'source'">
      <EmptyState v-if="sourceData.audits.length < 2" icon="🌐" title="需要更多审计数据" description="完成至少2次审计后，此处将展示来源权威趋势" />
      <template v-else>
        <div class="chart-card">
          <div class="section-title">引用域名趋势</div>
          <VChart :option="sourceTrendChartOption" :autoresize="true" style="height: 360px" />
        </div>
        <div class="two-col">
          <div class="chart-card">
            <div class="section-title">平台来源偏好</div>
            <VChart v-if="sourceData.platform_preferences.length > 0" :option="platformSourceChartOption" :autoresize="true" style="height: 300px" />
            <div v-else class="no-data-sm">暂无平台来源数据</div>
          </div>
          <div class="chart-card">
            <div class="section-title">权威度趋势</div>
            <table v-if="Object.values(sourceData.authority_trend).some(v => v.length > 0)" class="trend-table">
              <thead>
                <tr><th>域名</th><th>趋势</th></tr>
              </thead>
              <tbody>
                <template v-for="(domains, direction) in sourceData.authority_trend" :key="direction">
                  <tr v-for="domain in domains" :key="domain">
                    <td>{{ domain }}</td>
                    <td>
                      <span class="trend-badge" :class="direction === 'improving' ? 'trend-up' : direction === 'declining' ? 'trend-down' : 'trend-flat'">
                        {{ direction === 'improving' ? '↑ 上升' : direction === 'declining' ? '↓ 下降' : '→ 稳定' }}
                      </span>
                    </td>
                  </tr>
                </template>
              </tbody>
            </table>
            <div v-else class="no-data-sm">暂无权威度趋势数据</div>
          </div>
        </div>
      </template>
    </template>

    <!-- Tab 2: Competitor Positioning Map -->
    <template v-else-if="activeTab === 'positioning'">
      <EmptyState v-if="positioningData.brands.length === 0" icon="🗺️" title="暂无品牌数据" description="请先在项目中添加品牌和竞品" />
      <template v-else>
        <div class="chart-card">
          <div class="section-title">品牌定位象限图</div>
          <VChart :option="positioningChartOption" :autoresize="true" style="height: 400px" />
        </div>
        <div class="chart-card" style="margin-top: 16px">
          <div class="section-title">品牌提及趋势</div>
          <VChart :option="trajectoryChartOption" :autoresize="true" style="height: 300px" />
        </div>
      </template>
    </template>

    <!-- Tab 3: Answer Structure Evolution -->
    <template v-else-if="activeTab === 'structure'">
      <EmptyState v-if="structureData.audits.length < 2" icon="📋" title="需要更多审计数据" description="完成至少2次审计后，此处将展示回答结构演变" />
      <template v-else>
        <div class="chart-card">
          <div class="section-title">回答结构分布演变</div>
          <VChart :option="structureChartOption" :autoresize="true" style="height: 360px" />
        </div>
        <div class="two-col">
          <div class="chart-card">
            <div class="section-title">平台 × 结构类型</div>
            <VChart v-if="Object.keys(structureData.platform_structure).length > 0" :option="platformHeatmapOption" :autoresize="true" style="height: 280px" />
            <div v-else class="no-data-sm">暂无平台结构数据</div>
          </div>
          <div class="chart-card">
            <div class="section-title">结构类型与提及率</div>
            <table v-if="Object.keys(structureData.correlation).length > 0" class="trend-table">
              <thead>
                <tr><th>结构类型</th><th>提及率</th><th>平均位置</th></tr>
              </thead>
              <tbody>
                <tr v-for="(data, type) in structureData.correlation" :key="type">
                  <td>{{ structureTypeLabels[type] || type }}</td>
                  <td>{{ (data.mention_rate * 100).toFixed(1) }}%</td>
                  <td>{{ data.avg_position ?? '-' }}</td>
                </tr>
              </tbody>
            </table>
            <div v-else class="no-data-sm">暂无相关性数据</div>
            <div v-if="structureData.transitions.length > 0" style="margin-top: 16px">
              <div class="section-title">结构变化记录</div>
              <div class="transition-list">
                <div v-for="(t, i) in structureData.transitions.slice(0, 10)" :key="i" class="transition-item">
                  <span class="transition-platform">{{ platformLabels[t.platform] || t.platform }}</span>
                  <span class="transition-arrow">{{ structureTypeLabels[t.prev_structure ?? ''] || t.prev_structure }}</span>
                  <span class="transition-icon">→</span>
                  <span class="transition-arrow">{{ structureTypeLabels[t.new_structure] || t.new_structure }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </template>
    </template>

    <!-- Tab 4: Multi-Audit Comparison -->
    <template v-else-if="activeTab === 'compare'">
      <div class="compare-selector">
        <div class="section-title">选择审计进行对比（2-5个）</div>
        <div class="selector-row">
          <el-select
            v-model="selectedAuditIds"
            multiple
            placeholder="选择审计"
            style="width: 400px"
            :max-collapse-tags="5"
          >
            <el-option
              v-for="audit in auditsHistory.filter((a: any) => a.status === 'completed' || a.status === 'partial')"
              :key="audit.id"
              :label="`审计 #${audit.id} — ${audit.created_at?.slice(0, 16).replace('T', ' ')}`"
              :value="audit.id"
            />
          </el-select>
          <button class="compare-btn" :disabled="selectedAuditIds.length < 2" @click="runComparison">
            开始对比
          </button>
        </div>
      </div>

      <EmptyState v-if="compareData.audits.length === 0" icon="⚖️" title="选择审计开始对比" description="选择2-5个已完成的审计进行对比分析" />
      <template v-else>
        <!-- Summary Diff -->
        <div v-if="compareData.diffs" class="diff-row">
          <div class="diff-card">
            <div class="diff-label">评分变化</div>
            <div class="diff-value" :class="compareData.diffs.score_delta >= 0 ? 'diff-up' : 'diff-down'">
              {{ compareData.diffs.score_delta >= 0 ? '+' : '' }}{{ compareData.diffs.score_delta }}
            </div>
          </div>
          <div class="diff-card">
            <div class="diff-label">提及率变化</div>
            <div class="diff-value" :class="compareData.diffs.mention_rate_delta >= 0 ? 'diff-up' : 'diff-down'">
              {{ compareData.diffs.mention_rate_delta >= 0 ? '+' : '' }}{{ (compareData.diffs.mention_rate_delta * 100).toFixed(1) }}%
            </div>
          </div>
          <div class="diff-card">
            <div class="diff-label">新增来源</div>
            <div class="diff-value diff-up">{{ compareData.diffs.source_changes?.added?.length || 0 }}</div>
          </div>
          <div class="diff-card">
            <div class="diff-label">减少来源</div>
            <div class="diff-value diff-down">{{ compareData.diffs.source_changes?.removed?.length || 0 }}</div>
          </div>
        </div>

        <!-- Audit Snapshots -->
        <div class="snapshots-row">
          <div v-for="snapshot in compareData.audits" :key="snapshot.audit_id" class="snapshot-card">
            <div class="snapshot-header">审计 #{{ snapshot.audit_id }}</div>
            <div class="snapshot-date">{{ snapshot.date }}</div>
            <div class="snapshot-metrics">
              <div class="snapshot-metric">
                <span class="metric-label">评分</span>
                <span class="metric-value">{{ snapshot.overall_score }}</span>
              </div>
              <div class="snapshot-metric">
                <span class="metric-label">提及率</span>
                <span class="metric-value">{{ (snapshot.mention_rate * 100).toFixed(1) }}%</span>
              </div>
            </div>
            <div class="snapshot-sentiment">
              <span v-for="(count, sent) in snapshot.sentiment_breakdown" :key="sent" class="sentiment-tag" :class="'sent-' + sent">
                {{ sent === 'positive' ? '正面' : sent === 'negative' ? '负面' : '中性' }}: {{ count }}
              </span>
            </div>
          </div>
        </div>

        <!-- Source Changes -->
        <div v-if="compareData.diffs?.source_changes" class="chart-card" style="margin-top: 16px">
          <div class="section-title">来源变化</div>
          <div class="source-changes">
            <div v-if="compareData.diffs.source_changes.added?.length" class="source-group">
              <div class="source-label added">新增来源</div>
              <span v-for="s in compareData.diffs.source_changes.added" :key="s" class="source-tag added">{{ s }}</span>
            </div>
            <div v-if="compareData.diffs.source_changes.removed?.length" class="source-group">
              <div class="source-label removed">减少来源</div>
              <span v-for="s in compareData.diffs.source_changes.removed" :key="s" class="source-tag removed">{{ s }}</span>
            </div>
          </div>
        </div>

        <!-- Competitor Delta Chart -->
        <div v-if="compareData.diffs?.competitor_changes?.length" class="chart-card" style="margin-top: 16px">
          <div class="section-title">品牌提及率变化</div>
          <VChart :option="compareDeltaChartOption" :autoresize="true" style="height: 260px" />
        </div>
      </template>
    </template>
  </div>
</template>

<style scoped>
.strategic-page {
  max-width: 1200px;
}

.header {
  margin-bottom: 20px;
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

/* Tab Bar */
.tab-bar {
  display: flex;
  gap: 6px;
  margin-bottom: 20px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 10px;
}

.tab-btn {
  padding: 7px 16px;
  border-radius: 6px;
  font-size: 12px;
  background: transparent;
  color: var(--text-secondary);
  border: 1px solid transparent;
  cursor: pointer;
  transition: all 0.15s;
  display: flex;
  align-items: center;
  gap: 6px;
}

.tab-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.tab-btn.active {
  background: var(--accent-dim);
  color: var(--accent);
  border-color: var(--accent);
}

.tab-icon {
  font-size: 13px;
}

/* Cards */
.chart-card {
  background: var(--bg-card);
  border-radius: var(--radius-md);
  padding: 18px;
  border: 1px solid var(--border-light);
}

.section-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 14px;
}

.two-col {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-top: 16px;
}

/* Tables */
.trend-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.trend-table th {
  text-align: left;
  padding: 8px;
  color: var(--text-muted);
  font-weight: 500;
  border-bottom: 1px solid var(--border);
  font-size: 10px;
  text-transform: uppercase;
}

.trend-table td {
  padding: 10px 8px;
  border-bottom: 1px solid var(--border-light);
}

.trend-badge {
  font-size: 11px;
  font-weight: 600;
}

.trend-up { color: var(--status-good); }
.trend-down { color: var(--status-bad); }
.trend-flat { color: var(--text-muted); }

.no-data-sm {
  text-align: center;
  padding: 24px;
  color: var(--text-muted);
  font-size: 12px;
}

/* Tab 4: Compare */
.compare-selector {
  background: var(--bg-card);
  border-radius: var(--radius-md);
  padding: 18px;
  border: 1px solid var(--border-light);
  margin-bottom: 16px;
}

.selector-row {
  display: flex;
  gap: 12px;
  align-items: center;
}

.compare-btn {
  padding: 8px 20px;
  border-radius: 6px;
  font-size: 12px;
  background: var(--accent);
  color: #fff;
  border: none;
  cursor: pointer;
  font-weight: 600;
  transition: opacity 0.15s;
}

.compare-btn:disabled {
  opacity: 0.4;
  cursor: default;
}

.compare-btn:not(:disabled):hover {
  opacity: 0.85;
}

/* Diff Row */
.diff-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
  margin-bottom: 16px;
}

.diff-card {
  background: var(--bg-card);
  border-radius: var(--radius-md);
  padding: 16px 18px;
  border: 1px solid var(--border-light);
}

.diff-label {
  font-size: 11px;
  color: var(--text-muted);
  margin-bottom: 8px;
}

.diff-value {
  font-size: 28px;
  font-weight: 700;
  line-height: 1;
}

.diff-up { color: var(--status-good); }
.diff-down { color: var(--status-bad); }

/* Snapshots */
.snapshots-row {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 14px;
}

.snapshot-card {
  background: var(--bg-card);
  border-radius: var(--radius-md);
  padding: 16px;
  border: 1px solid var(--border-light);
}

.snapshot-header {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.snapshot-date {
  font-size: 11px;
  color: var(--text-muted);
  margin: 6px 0 12px;
}

.snapshot-metrics {
  display: flex;
  gap: 16px;
  margin-bottom: 10px;
}

.snapshot-metric {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.metric-label {
  font-size: 10px;
  color: var(--text-muted);
}

.metric-value {
  font-size: 18px;
  font-weight: 700;
  color: var(--text-primary);
}

.snapshot-sentiment {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.sentiment-tag {
  font-size: 10px;
  padding: 2px 8px;
  border-radius: 10px;
  font-weight: 600;
}

.sent-positive { background: rgba(0,212,170,0.12); color: var(--status-good); }
.sent-neutral { background: rgba(113,113,122,0.12); color: var(--text-muted); }
.sent-negative { background: rgba(239,68,68,0.12); color: var(--status-bad); }

/* Source Changes */
.source-changes {
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
}

.source-group {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.source-label {
  font-size: 11px;
  font-weight: 600;
  margin-right: 4px;
}

.source-label.added { color: var(--status-good); }
.source-label.removed { color: var(--status-bad); }

.source-tag {
  font-size: 11px;
  padding: 2px 10px;
  border-radius: 10px;
}

.source-tag.added { background: rgba(0,212,170,0.12); color: var(--status-good); }
.source-tag.removed { background: rgba(239,68,68,0.12); color: var(--status-bad); }

/* Transitions */
.transition-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.transition-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  padding: 4px 0;
}

.transition-platform {
  font-weight: 500;
  color: var(--text-primary);
  min-width: 60px;
}

.transition-arrow {
  color: var(--text-secondary);
}

.transition-icon {
  color: var(--accent);
}

@media (max-width: 768px) {
  .two-col { grid-template-columns: 1fr; }
  .diff-row { grid-template-columns: repeat(2, 1fr); }
  .tab-bar { flex-wrap: wrap; }
  .selector-row { flex-direction: column; align-items: flex-start; }
}
</style>
