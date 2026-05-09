<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import {
  TitleComponent,
  TooltipComponent,
  GridComponent,
  LegendComponent,
  DataZoomComponent,
} from 'echarts/components'
import VChart from 'vue-echarts'
import { getTrendData, type TrendPoint } from '../../api/client'

use([
  CanvasRenderer,
  LineChart,
  TitleComponent,
  TooltipComponent,
  GridComponent,
  LegendComponent,
  DataZoomComponent,
])

const props = defineProps<{
  projectId: number
}>()

const trendData = ref<TrendPoint[]>([])
const period = ref<'daily' | 'weekly' | 'monthly'>('daily')

const chartOption = computed(() => {
  const dates = trendData.value.map(d => d.date)
  const scores = trendData.value.map(d => d.overall_score)
  const mentionRates = trendData.value.map(d => Math.round(d.mention_rate * 100))

  // Extract platform series
  const platformNames = new Set<string>()
  trendData.value.forEach(d => {
    Object.keys(d.platform_scores || {}).forEach(p => platformNames.add(p))
  })

  const platformSeries = Array.from(platformNames).map(platform => ({
    name: platformLabel[platform] || platform,
    type: 'line' as const,
    smooth: true,
    symbol: 'circle',
    symbolSize: 4,
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
      data: ['综合评分', '提及率(%)', ...Array.from(platformNames).map(p => platformLabel[p] || p)],
      textStyle: { color: '#71717a', fontSize: 10 },
      bottom: 0,
    },
    grid: {
      left: 40,
      right: 16,
      top: 16,
      bottom: 40,
    },
    xAxis: {
      type: 'category',
      data: dates,
      axisLine: { lineStyle: { color: '#27272a' } },
      axisLabel: { color: '#52525b', fontSize: 10, rotate: dates.length > 10 ? 45 : 0 },
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
        lineStyle: { color: '#00d4aa', width: 2 },
        itemStyle: { color: '#00d4aa' },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(0,212,170,0.2)' },
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

const platformLabel: Record<string, string> = {
  deepseek: 'DeepSeek',
  qwen: '通义千问',
  doubao: '豆包',
  kimi: 'Kimi',
  wenxin: '文心一言',
  hunyuan: '腾讯元宝',
}

async function fetchTrend() {
  try {
    const { data } = await getTrendData(props.projectId, period.value, 30)
    trendData.value = data.data
  } catch {
    trendData.value = []
  }
}

watch(() => props.projectId, fetchTrend)
watch(period, fetchTrend)
onMounted(fetchTrend)
</script>

<template>
  <div class="chart-card">
    <div class="section-title">
      可见性趋势
      <div class="period-tabs">
        <button
          v-for="p in (['daily', 'weekly', 'monthly'] as const)"
          :key="p"
          class="tab"
          :class="{ active: period === p }"
          @click="period = p"
        >
          {{ p === 'daily' ? '日' : p === 'weekly' ? '周' : '月' }}
        </button>
      </div>
    </div>
    <VChart
      v-if="trendData.length > 0"
      :option="chartOption"
      :autoresize="true"
      style="height: 220px"
    />
    <div v-else class="chart-empty">
      <span class="empty-icon">📈</span>
      <span>完成审计后显示趋势数据</span>
    </div>
  </div>
</template>

<style scoped>
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
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.period-tabs {
  display: flex;
  gap: 4px;
}

.tab {
  padding: 3px 10px;
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

.chart-empty {
  height: 180px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--text-muted);
  font-size: 11px;
  background: var(--bg-input);
  border-radius: var(--radius-sm);
  border: 1px dashed var(--border);
}

.empty-icon {
  font-size: 24px;
  opacity: 0.5;
}
</style>
