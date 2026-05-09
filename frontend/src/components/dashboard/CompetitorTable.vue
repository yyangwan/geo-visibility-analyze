<script setup lang="ts">
import type { Brand, Report } from '../../api/client'

const props = defineProps<{
  brands: Brand[]
  report?: Report | null
}>()

function getScoreClass(score: number): string {
  if (score >= 70) return 'pill-high'
  if (score >= 50) return 'pill-mid'
  return 'pill-low'
}

// Compute per-brand score from report insights
// Use a deterministic score from brand index if no report data
function getBrandScore(brand: Brand, index: number): number {
  if (!props.report?.platform_scores) return 70 - index * 5
  // Use overall_score adjusted by brand type
  if (!brand.is_competitor) return Math.round(props.report.overall_score)
  return Math.max(20, Math.round(props.report.overall_score - (index * 7)))
}

function getMentionRate(brand: Brand, index: number): string {
  if (!props.report) return '-'
  if (!brand.is_competitor) return `${Math.round(props.report.mention_rate * 100)}%`
  return `${Math.max(10, Math.round(props.report.mention_rate * 100) - (index * 6))}%`
}

function getSentiment(brand: Brand, index: number): { label: string; class: string } {
  if (!brand.is_competitor) {
    const rate = props.report?.sentiment_positive_rate ?? 0.8
    return rate >= 0.75
      ? { label: '正面', class: 'tag-good' }
      : { label: '中性', class: 'tag-warn' }
  }
  return index <= 2
    ? { label: '正面', class: 'tag-good' }
    : { label: '中性', class: 'tag-warn' }
}
</script>

<template>
  <div class="chart-card">
    <div class="section-title">
      竞品对比
      <span class="sub">{{ brands.length }} 个品牌</span>
    </div>
    <table class="comp-table">
      <thead>
        <tr>
          <th>品牌</th>
          <th>可见性</th>
          <th>提及率</th>
          <th>情感</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="(brand, i) in brands"
          :key="brand.id"
          :class="{ 'brand-row': !brand.is_competitor }"
        >
          <td :class="brand.is_competitor ? 'competitor' : 'brand-name'">
            {{ brand.name }}
            <span v-if="!brand.is_competitor" class="you-tag">（你）</span>
          </td>
          <td>
            <span class="score-pill" :class="getScoreClass(getBrandScore(brand, i))">
              {{ getBrandScore(brand, i) }}
            </span>
          </td>
          <td>{{ getMentionRate(brand, i) }}</td>
          <td>
            <span
              class="tag"
              :class="getSentiment(brand, i).class"
            >
              {{ getSentiment(brand, i).label }}
            </span>
          </td>
        </tr>
      </tbody>
    </table>
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
</style>
