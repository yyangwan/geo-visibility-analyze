<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { RadarChart, BarChart } from 'echarts/charts'
import { TitleComponent, TooltipComponent, LegendComponent, GridComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import { useProjectStore } from '../../stores/project'
import { getLatestReport, getAuditResults, type QueryResult } from '../../api/client'

use([CanvasRenderer, RadarChart, BarChart, TitleComponent, TooltipComponent, LegendComponent, GridComponent])

const store = useProjectStore()
const results = ref<QueryResult[]>([])
const loading = ref(false)

const platformNames: Record<string, string> = {
  deepseek: 'DeepSeek',
  qwen: '通义千问',
  doubao: '豆包',
  kimi: 'Kimi',
  wenxin: '文心一言',
  hunyuan: '腾讯元宝',
}

const radarOption = computed(() => {
  const platforms = Object.keys(platformNames)
  const brands = store.brands.filter(b => !b.is_competitor).map(b => b.name)
  const competitors = store.brands.filter(b => b.is_competitor).map(b => b.name)
  const allBrands = [...brands, ...competitors]

  // Group results by brand, then compute per-platform mention rate
  const brandData: Record<string, Record<string, { total: number; mentions: number }>> = {}
  for (const r of results.value) {
    if (!r.brand_name || !r.mention_found) continue
    if (!brandData[r.brand_name]) brandData[r.brand_name] = {}
    if (!brandData[r.brand_name][r.platform]) {
      brandData[r.brand_name][r.platform] = { total: 0, mentions: 0 }
    }
    brandData[r.brand_name][r.platform].mentions++
  }

  // Count total queries per platform
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
  try {
    const { data: report } = await getLatestReport(store.currentProject.id)
    if (report?.audit_id) {
      const { data } = await getAuditResults(report.audit_id)
      results.value = data
    }
  } catch {
    results.value = []
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

    <div v-if="results.length > 0" class="content">
      <!-- Radar Chart -->
      <div class="chart-card">
        <div class="section-title">平台维度雷达图</div>
        <VChart :option="radarOption" :autoresize="true" style="height: 320px" />
      </div>

      <!-- Bar Chart -->
      <div class="chart-card">
        <div class="section-title">品牌提及率排行</div>
        <VChart :option="barOption" :autoresize="true" style="height: 280px" />
      </div>

      <!-- Detail Table -->
      <div class="chart-card">
        <div class="section-title">详细结果</div>
        <table class="detail-table">
          <thead>
            <tr>
              <th>平台</th>
              <th>Prompt</th>
              <th>品牌</th>
              <th>提及</th>
              <th>推荐</th>
              <th>置信度</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in results.slice(0, 50)" :key="r.id">
              <td>{{ platformNames[r.platform] || r.platform }}</td>
              <td class="prompt-cell">{{ r.prompt_text?.slice(0, 40) || '-' }}{{ (r.prompt_text?.length || 0) > 40 ? '...' : '' }}</td>
              <td>{{ r.brand_name }}</td>
              <td>
                <span :class="r.mention_found ? 'tag-good' : 'tag-bad'" class="tag">
                  {{ r.mention_found ? '是' : '否' }}
                </span>
              </td>
              <td>
                <span v-if="r.is_recommended" class="tag tag-accent">推荐</span>
                <span v-else class="text-muted">-</span>
              </td>
              <td>{{ r.mention_confidence ? (r.mention_confidence * 100).toFixed(0) + '%' : '-' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-else class="empty">
      <div class="empty-icon">⚔️</div>
      <h3>暂无竞品对比数据</h3>
      <p>完成首次审计后，此处将展示多维度竞品分析</p>
    </div>
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

.content {
  display: flex;
  flex-direction: column;
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

.detail-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.detail-table th {
  text-align: left;
  padding: 8px 10px;
  color: var(--text-muted);
  font-weight: 500;
  border-bottom: 1px solid var(--border);
  font-size: 10px;
  text-transform: uppercase;
}

.detail-table td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--border-light);
}

.detail-table tr:hover td { background: var(--bg-hover); }

.prompt-cell {
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
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

.empty {
  text-align: center;
  padding: 60px 20px;
  color: var(--text-muted);
}

.empty-icon { font-size: 48px; margin-bottom: 16px; }
.empty h3 { font-size: 16px; color: var(--text-primary); margin-bottom: 8px; }
.empty p { font-size: 13px; }
</style>
