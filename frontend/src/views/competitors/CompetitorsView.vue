<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { RadarChart, BarChart } from 'echarts/charts'
import { TitleComponent, TooltipComponent, LegendComponent, GridComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import { useProjectStore } from '../../stores/project'
import { PLATFORM_LABELS } from '../../constants/platforms'
import { ElMessage } from 'element-plus'
import { getLatestReport, getAuditResults, type QueryResult } from '../../api/client'
import LoadingSkeleton from '../../components/common/LoadingSkeleton.vue'
import ErrorState from '../../components/common/ErrorState.vue'
import EmptyState from '../../components/common/EmptyState.vue'

use([CanvasRenderer, RadarChart, BarChart, TitleComponent, TooltipComponent, LegendComponent, GridComponent])

const store = useProjectStore()
const results = ref<QueryResult[]>([])
const loading = ref(false)
const error = ref('')
const filterPlatform = ref('all')
const filterMentionOnly = ref(false)

const platformNames = PLATFORM_LABELS
const platformKeys = Object.keys(platformNames)

const filteredResults = computed(() => {
  let list = results.value
  if (filterPlatform.value !== 'all') {
    list = list.filter(r => r.platform === filterPlatform.value)
  }
  if (filterMentionOnly.value) {
    list = list.filter(r => r.mention_found)
  }
  return list
})

const mentionCount = computed(() => {
  const base = filterPlatform.value === 'all' ? results.value : results.value.filter(r => r.platform === filterPlatform.value)
  return base.filter(r => r.mention_found).length
})

const radarOption = computed(() => {
  const platforms = Object.keys(platformNames)
  const brands = store.brands.filter(b => !b.is_competitor).map(b => b.name)
  const competitors = store.brands.filter(b => b.is_competitor).map(b => b.name)
  const allBrands = [...brands, ...competitors]

  const brandData: Record<string, Record<string, { total: number; mentions: number }>> = {}
  for (const r of results.value) {
    if (!r.brand_name || !r.mention_found) continue
    if (!brandData[r.brand_name]) brandData[r.brand_name] = {}
    if (!brandData[r.brand_name][r.platform]) {
      brandData[r.brand_name][r.platform] = { total: 0, mentions: 0 }
    }
    brandData[r.brand_name][r.platform].mentions++
  }

  const platformTotals: Record<string, number> = {}
  for (const r of results.value) {
    platformTotals[r.platform] = (platformTotals[r.platform] || 0) + 1
  }

  const series = allBrands.map(brand => ({
    name: brand,
    value: platforms.map(p => {
      const mentions = brandData[brand]?.[p]?.mentions || 0
      const total = platformTotals[p] || 1
      return Math.round((mentions / total) * 100)
    }),
  }))

  return {
    tooltip: {
      backgroundColor: '#1a1d29',
      borderColor: '#27272a',
      textStyle: { color: '#e4e4e7', fontSize: 12 },
    },
    legend: {
      data: allBrands,
      textStyle: { color: '#71717a', fontSize: 10 },
      bottom: 0,
    },
    radar: {
      indicator: platforms.map(p => ({ name: platformNames[p] || p, max: 100 })),
      shape: 'polygon',
      axisName: { color: '#71717a', fontSize: 10 },
      splitArea: { areaStyle: { color: ['rgba(0,0,0,0)', 'rgba(0,0,0,0.02)'] } },
      splitLine: { lineStyle: { color: '#27272a' } },
      axisLine: { lineStyle: { color: '#27272a' } },
    },
    series: [{
      type: 'radar',
      data: series.map((s, i) => ({
        name: s.name,
        value: s.value,
        symbol: 'circle',
        symbolSize: 4,
        lineStyle: {
          color: i === 0 ? '#00d4aa' : '#4cc9f0',
          width: 2,
        },
        itemStyle: {
          color: i === 0 ? '#00d4aa' : '#4cc9f0',
        },
        areaStyle: {
          color: i === 0 ? 'rgba(0,212,170,0.15)' : 'rgba(76,201,240,0.08)',
        },
      })),
    }],
  }
})

const barOption = computed(() => {
  const brandMentions: Record<string, number> = {}
  const brandTotals: Record<string, number> = {}
  for (const r of results.value) {
    if (!r.brand_name) continue
      brandTotals[r.brand_name] = (brandTotals[r.brand_name] || 0) + 1
    if (r.mention_found) {
      brandMentions[r.brand_name] = (brandMentions[r.brand_name] || 0) + 1
    }
  }

  const sortedBrands = Object.keys(brandTotals).sort(
    (a, b) => (brandMentions[b] || 0) - (brandMentions[a] || 0)
  )

  return {
    tooltip: {
      backgroundColor: '#1a1d29',
      borderColor: '#27272a',
      textStyle: { color: '#e4e4e7', fontSize: 12 },
    },
    grid: { left: 80, right: 16, top: 16, bottom: 30 },
    xAxis: {
      type: 'value',
      max: 100,
      axisLabel: { color: '#52525b', fontSize: 10 },
      splitLine: { lineStyle: { color: '#1e1e24' } },
    },
    yAxis: {
      type: 'category',
      data: sortedBrands,
      axisLabel: { color: '#e4e4e7', fontSize: 11 },
      axisLine: { lineStyle: { color: '#27272a' } },
    },
    series: [{
      type: 'bar',
      data: sortedBrands.map(brand => {
        const rate = brandTotals[brand] ? Math.round((brandMentions[brand] || 0) / brandTotals[brand] * 100) : 0
        const isPrimary = store.brands.find(b => b.name === brand && !b.is_competitor)
        return {
          value: rate,
          itemStyle: {
            color: isPrimary ? '#00d4aa' : '#4cc9f0',
            borderRadius: [0, 4, 4, 0],
          },
        }
      }),
      barWidth: 16,
    }],
  }
})

async function loadData() {
  if (!store.currentProject) return
  loading.value = true
  error.value = ''
  try {
    const { data: report } = await getLatestReport(store.currentProject.id)
    if (report?.audit_id) {
      const { data } = await getAuditResults(report.audit_id)
      results.value = data
    }
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '加载竞品数据失败'
    ElMessage.error(error.value)
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  if (!store.currentProject) await store.fetchProjects()
  loadData()
})
</script>

<template>
  <div>
    <div class="header">
      <h1>竞品对比</h1>
      <button class="btn btn-primary" @click="loadData" :disabled="loading">
        {{ loading ? '加载中...' : '刷新数据' }}
      </button>
    </div>

    <LoadingSkeleton v-if="loading" variant="chart" />
    <ErrorState v-else-if="error" :message="error" @retry="loadData" />

    <div v-else-if="results.length > 0" class="content">
      <!-- Charts Row -->
      <div class="charts-row">
        <div class="chart-card chart-radar">
          <div class="section-title">平台维度雷达图</div>
          <VChart :option="radarOption" :autoresize="true" style="height: 300px" />
        </div>
        <div class="chart-card chart-bar">
          <div class="section-title">品牌提及率排行</div>
          <VChart :option="barOption" :autoresize="true" style="height: 300px" />
        </div>
      </div>

      <!-- Detail Table -->
      <div class="chart-card">
        <div class="table-toolbar">
          <div class="section-title">详细结果 <span class="count">{{ filteredResults.length }} / {{ results.length }}</span></div>
          <div class="filter-bar">
            <div class="filter-chips">
              <button
                class="chip"
                :class="{ active: filterPlatform === 'all' }"
                @click="filterPlatform = 'all'"
              >全部</button>
              <button
                v-for="p in platformKeys"
                :key="p"
                class="chip"
                :class="{ active: filterPlatform === p }"
                @click="filterPlatform = p"
              >{{ platformNames[p] }}</button>
            </div>
            <label class="toggle-label">
              <input type="checkbox" v-model="filterMentionOnly" />
              <span>仅看提及</span>
              <span class="mention-badge">{{ mentionCount }}</span>
            </label>
          </div>
        </div>
        <div class="table-scroll">
          <table class="detail-table">
            <thead>
              <tr>
                <th class="col-platform">平台</th>
                <th class="col-prompt">Prompt</th>
                <th class="col-brand">品牌</th>
                <th class="col-mention">提及</th>
                <th class="col-rec">推荐</th>
                <th class="col-conf">置信度</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in filteredResults" :key="r.id" :class="{ 'row-error': r.error, 'row-mention': r.mention_found }">
                <td>
                  <span class="platform-dot" :style="{ background: `var(--plat-${r.platform}, #71717a)` }"></span>
                  {{ platformNames[r.platform] || r.platform }}
                </td>
                <td class="prompt-cell" :title="r.prompt_text">{{ r.prompt_text || '-' }}</td>
                <td>{{ r.brand_name }}</td>
                <td>
                  <span :class="r.mention_found ? 'tag-good' : 'tag-bad'" class="tag">
                    {{ r.mention_found ? '是' : '否' }}
                  </span>
                </td>
                <td>
                  <span v-if="r.is_recommended" class="tag tag-accent">
                    推荐<span v-if="r.recommendation_rank"> #{{ r.recommendation_rank }}</span>
                  </span>
                  <span v-else class="text-muted">-</span>
                </td>
                <td>{{ r.mention_confidence ? (r.mention_confidence * 100).toFixed(0) + '%' : '-' }}</td>
              </tr>
              <tr v-if="filteredResults.length === 0">
                <td colspan="6" class="empty-hint">无匹配结果，试试调整筛选条件</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <EmptyState
      v-else
      icon="⚔️"
      title="暂无竞品对比数据"
      description="完成首次审计后，此处将展示多维度竞品分析"
    />
  </div>
</template>

<style scoped>
.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 24px;
}

.header h1 { font-size: 18px; font-weight: 600; }

.content {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.charts-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

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

.count {
  font-weight: 400;
  color: var(--text-muted);
  font-size: 11px;
}

/* --- Table Toolbar --- */
.table-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  flex-wrap: wrap;
}

.table-toolbar .section-title {
  margin-bottom: 0;
  line-height: 28px;
}

.filter-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.filter-chips {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.chip {
  padding: 4px 12px;
  border-radius: 14px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-secondary);
  font-size: 11px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s ease;
  white-space: nowrap;
}

.chip:hover {
  border-color: var(--accent);
  color: var(--text-primary);
}

.chip.active {
  background: var(--accent-dim);
  border-color: var(--accent);
  color: var(--accent);
}

.toggle-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--text-secondary);
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
}

.toggle-label input {
  accent-color: var(--accent);
  width: 14px;
  height: 14px;
}

.mention-badge {
  background: var(--accent-dim);
  color: var(--accent);
  font-size: 10px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 10px;
}

/* --- Table --- */
.table-scroll {
  max-height: 520px;
  overflow-y: auto;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  margin-top: 12px;
  border-radius: 6px;
  border: 1px solid var(--border-light);
}

.detail-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.detail-table thead {
  position: sticky;
  top: 0;
  z-index: 1;
}

.detail-table th {
  text-align: left;
  padding: 10px 12px;
  color: var(--text-muted);
  font-weight: 500;
  border-bottom: 1px solid var(--border);
  font-size: 10px;
  text-transform: uppercase;
  background: var(--bg-card);
  letter-spacing: 0.3px;
}

.col-platform { width: 100px; }
.col-prompt { min-width: 180px; }
.col-brand { width: 90px; }
.col-mention { width: 60px; text-align: center; }
.col-rec { width: 80px; text-align: center; }
.col-conf { width: 70px; text-align: center; }

.detail-table td {
  padding: 8px 12px;
  border-bottom: 1px solid var(--border-light);
}

.detail-table tbody tr:hover td { background: var(--bg-hover); }

.row-error td { opacity: 0.5; }
.row-mention td { background: rgba(0, 212, 170, 0.03); }

.platform-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  margin-right: 6px;
  vertical-align: middle;
}

.prompt-cell {
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
}

.empty-hint {
  text-align: center;
  padding: 32px 12px !important;
  color: var(--text-muted);
  font-size: 12px;
}

.tag {
  font-size: 9px;
  padding: 1px 6px;
  border-radius: 10px;
  font-weight: 600;
}

.tag-good { background: rgba(0,212,170,0.12); color: var(--status-good); }
.tag-bad { background: rgba(239,68,68,0.12); color: var(--status-bad); }
.tag-accent { background: rgba(76,201,240,0.12); color: var(--accent-blue); }

.text-muted { color: var(--text-muted); }

@media (max-width: 768px) {
  .charts-row { grid-template-columns: 1fr; }
  .table-toolbar { flex-direction: column; }
  .filter-bar { width: 100%; }
  .table-scroll { max-height: 400px; }
}
</style>
