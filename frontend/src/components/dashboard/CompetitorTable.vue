<script setup lang="ts">
import { computed } from 'vue'
import type { Brand, Report, QueryResult } from '../../api/client'

const props = defineProps<{
  brands: Brand[]
  report?: Report | null
  results?: QueryResult[]
}>()

interface BrandRow {
  id: number
  name: string
  is_competitor: boolean
  mentionRate: number
  recommendRate: number
  avgConfidence: number
  score: number
}

const rows = computed(() => {
  if (!props.results || !props.results.length) return []

  return props.brands.map(brand => {
    const brandResults = props.results!.filter(r => r.brand_name === brand.name)
    const total = brandResults.length
    const mentions = brandResults.filter(r => r.mention_found).length
    const recommended = brandResults.filter(r => r.is_recommended).length
    const confidences = brandResults.filter(r => r.mention_confidence != null).map(r => r.mention_confidence!)

    const mentionRate = total > 0 ? mentions / total : 0
    const recommendRate = total > 0 ? recommended / total : 0
    const avgConfidence = confidences.length > 0
      ? confidences.reduce((a, b) => a + b, 0) / confidences.length
      : 0

    // Score: same formula as backend report_service
    const score = Math.round(mentionRate * 50 + recommendRate * 30 + avgConfidence * 20)

    return {
      id: brand.id,
      name: brand.name,
      is_competitor: brand.is_competitor,
      mentionRate,
      recommendRate,
      avgConfidence,
      score,
    } as BrandRow
  }).sort((a, b) => b.score - a.score)
})

function getScoreClass(score: number): string {
  if (score >= 70) return 'pill-high'
  if (score >= 50) return 'pill-mid'
  return 'pill-low'
}

function getSentiment(confidence: number): { label: string; class: string } {
  if (confidence >= 0.75) return { label: '正面', class: 'tag-good' }
  if (confidence >= 0.5) return { label: '中性', class: 'tag-warn' }
  return { label: '负面', class: 'tag-bad' }
}
</script>

<template>
  <div class="chart-card">
    <div class="section-title">
      竞品对比
      <span class="sub">{{ brands.length }} 个品牌</span>
    </div>
    <div v-if="rows.length > 0" class="table-scroll">
      <table class="comp-table">
        <thead>
          <tr>
            <th>品牌</th>
            <th>可见性</th>
            <th>提及率</th>
            <th>推荐率</th>
            <th>情感</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in rows"
            :key="row.id"
            :class="{ 'brand-row': !row.is_competitor }"
          >
            <td :class="row.is_competitor ? 'competitor' : 'brand-name'">
              {{ row.name }}
              <span v-if="!row.is_competitor" class="you-tag">（你）</span>
            </td>
            <td>
              <span class="score-pill" :class="getScoreClass(row.score)">
                {{ row.score }}
              </span>
            </td>
            <td>{{ Math.round(row.mentionRate * 100) }}%</td>
            <td>{{ Math.round(row.recommendRate * 100) }}%</td>
            <td>
              <span
                class="tag"
                :class="getSentiment(row.avgConfidence).class"
              >
                {{ getSentiment(row.avgConfidence).label }}
              </span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-else class="no-data">暂无对比数据</div>
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

.sub {
  font-size: 11px;
  color: var(--text-muted);
  font-weight: 400;
}

.comp-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.comp-table th {
  text-align: left;
  padding: 8px 10px;
  color: var(--text-muted);
  font-weight: 500;
  border-bottom: 1px solid var(--border);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
}

.comp-table td {
  padding: 10px;
  border-bottom: 1px solid var(--border-light);
}

.comp-table tr:hover td {
  background: var(--bg-hover);
}

.brand-row {
  background: var(--accent-dim);
}

.brand-row:hover td {
  background: var(--accent-dim) !important;
}

.brand-name {
  font-weight: 600;
  color: var(--accent);
}

.competitor {
  color: var(--text-secondary);
}

.you-tag {
  color: var(--accent);
  font-size: 10px;
}

.score-pill {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 600;
  font-family: "Inter", monospace;
}

.pill-high { background: rgba(0, 212, 170, 0.1); color: var(--status-good); }
.pill-mid { background: rgba(251, 191, 36, 0.1); color: var(--status-warn); }
.pill-low { background: rgba(239, 68, 68, 0.1); color: var(--status-bad); }

.tag {
  font-size: 9px;
  padding: 1px 6px;
  border-radius: 10px;
  font-weight: 600;
}

.tag-good { background: rgba(0, 212, 170, 0.12); color: var(--status-good); }
.tag-warn { background: rgba(251, 191, 36, 0.12); color: var(--status-warn); }
.tag-bad { background: rgba(239, 68, 68, 0.12); color: var(--status-bad); }

.no-data {
  text-align: center;
  padding: 24px;
  color: var(--text-muted);
  font-size: 12px;
}
</style>
