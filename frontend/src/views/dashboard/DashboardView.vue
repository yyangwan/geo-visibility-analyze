<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useProjectStore } from '../../stores/project'
import {
  createAudit,
  generateReport,
  getLatestReport,
  exportReportPdf,
  getTrendData,
  getSchedules,
} from '../../api/client'
import type { TrendPoint } from '../../api/client'
import ScoreCard from '../../components/dashboard/ScoreCard.vue'
import PlatformGrid from '../../components/dashboard/PlatformGrid.vue'
import CompetitorTable from '../../components/dashboard/CompetitorTable.vue'
import InsightCard from '../../components/dashboard/InsightCard.vue'
import TrendChart from '../../components/dashboard/TrendChart.vue'

const router = useRouter()
const store = useProjectStore()
const creating = ref(false)
const exporting = ref(false)
const previousScore = ref<number | null>(null)
const previousMention = ref<number | null>(null)
const nextScheduleTime = ref<string | null>(null)

const report = computed(() => store.report)
const hasData = computed(() => !!report.value)

const scoreCards = computed(() => {
  if (!report.value) return []
  const r = report.value

  const scoreChange = previousScore.value != null
    ? Math.round(r.overall_score - previousScore.value)
    : null
  const mentionChange = previousMention.value != null
    ? Math.round((r.mention_rate - previousMention.value) * 100)
    : null
  const sentimentRate = r.sentiment_positive_rate ?? 0

  return [
    {
      label: '综合可见性评分',
      value: r.overall_score,
      suffix: '/100',
      status: (r.overall_score >= 70 ? 'good' : r.overall_score >= 40 ? 'warn' : 'alert') as 'good' | 'warn' | 'alert',
      change: scoreChange != null ? (scoreChange >= 0 ? `+${scoreChange}%` : `${scoreChange}%`) : undefined,
      changeDir: (scoreChange ?? 0) > 0 ? 'up' as const : (scoreChange ?? 0) < 0 ? 'down' as const : 'flat' as const,
      benchmark: '行业均值 68',
    },
    {
      label: '品牌提及率',
      value: Math.round(r.mention_rate * 100),
      suffix: '%',
      status: (r.mention_rate >= 0.6 ? 'good' : r.mention_rate >= 0.4 ? 'warn' : 'alert') as 'good' | 'warn' | 'alert',
      change: mentionChange != null ? (mentionChange >= 0 ? `+${mentionChange}%` : `${mentionChange}%`) : undefined,
      changeDir: (mentionChange ?? 0) > 0 ? 'up' as const : (mentionChange ?? 0) < 0 ? 'down' as const : 'flat' as const,
      benchmark: '行业均值 61%',
    },
    {
      label: '竞品排名',
      value: r.competitor_rank ? `#${r.competitor_rank}` : '-',
      suffix: '',
      status: 'good' as const,
    },
    {
      label: '正向情感占比',
      value: r.sentiment_positive_rate ? Math.round(sentimentRate * 100) : '-',
      suffix: '%',
      status: (sentimentRate >= 0.75 ? 'good' : sentimentRate >= 0.6 ? 'warn' : 'alert') as 'good' | 'warn' | 'alert',
      alertBadge: sentimentRate > 0 && sentimentRate < 0.75 ? '下降' : undefined,
    },
  ]
})

// CTA: find weakest platform and missing scenario
const ctaInfo = computed(() => {
  if (!report.value || !report.value.platform_scores) return null
  const ps = report.value.platform_scores
  const entries = Object.entries(ps)
  if (entries.length === 0) return null
  const worst = entries.reduce((a, b) => a[1] < b[1] ? a : b)
  const platformLabels: Record<string, string> = {
    deepseek: 'DeepSeek', qwen: '通义千问', doubao: '豆包',
    kimi: 'Kimi', wenxin: '文心一言', hunyuan: '腾讯元宝',
  }
  return {
    platform: platformLabels[worst[0]] || worst[0],
    score: worst[1],
  }
})

async function handleExportPdf() {
  if (!report.value || exporting.value) return
  exporting.value = true
  try {
    const { data } = await exportReportPdf(report.value.id)
    const url = window.URL.createObjectURL(new Blob([data], { type: 'application/pdf' }))
    const a = document.createElement('a')
    a.href = url
    a.download = `report-${report.value.id}.pdf`
    a.click()
    window.URL.revokeObjectURL(url)
  } catch { /* ignore */ } finally {
    exporting.value = false
  }
}

async function handleNewAudit() {
  if (!store.currentProject) return
  creating.value = true
  try {
    const { data: audit } = await createAudit({ project_id: store.currentProject.id })
    let status = audit.status
    while (status === 'pending' || status === 'running') {
      await new Promise(r => setTimeout(r, 2000))
      const { data: updated } = await (await import('../../api/client')).default.get(`/audits/${audit.id}`)
      status = updated.status
    }
    await generateReport(audit.id)
    await store.fetchReport(audit.id)
  } finally {
    creating.value = false
  }
}

async function loadPreviousData() {
  if (!store.currentProject) return
  try {
    const { data } = await getTrendData(store.currentProject.id, 'daily', 30)
    const points: TrendPoint[] = data.data
    if (points.length >= 2) {
      const prev = points[points.length - 2]
      previousScore.value = prev.overall_score
      previousMention.value = prev.mention_rate
    }
  } catch { /* ignore */ }
}

async function loadScheduleInfo() {
  try {
    const { data } = await getSchedules()
    const active = data.filter((s: any) => s.is_active)
    if (active.length > 0) {
      // Parse cron to display a friendly time
      const cron = active[0].cron_expression
      const parts = cron.split(' ')
      if (parts.length >= 2) {
        nextScheduleTime.value = `每天 ${parts[1].padStart(2, '0')}:${parts[0].padStart(2, '0')}`
      }
    }
  } catch { /* ignore */ }
}

onMounted(async () => {
  await store.fetchProjects()
  if (store.currentProject) {
    try {
      const { data } = await getLatestReport(store.currentProject.id)
      store.report = data
    } catch { /* no report yet */ }
    loadPreviousData()
    loadScheduleInfo()
  }
})
</script>

<template>
  <div class="dashboard">
    <!-- Header -->
    <div class="header">
      <div>
        <h1>{{ store.currentProject?.name || 'AI Scope' }} · AI搜索可见性报告</h1>
        <div class="header-meta">
          <span v-if="hasData" class="freshness">
            <span class="dot-green"></span>
            基于 {{ store.prompts.length }} 条 Prompt · {{ store.report?.platform_scores ? Object.keys(store.report.platform_scores).length : 0 }} 个平台
          </span>
          <span v-else class="text-muted">暂无数据，请新建审计</span>
          <template v-if="hasData">
            <span class="meta-divider">|</span>
            <span v-if="nextScheduleTime" class="schedule-hint">下次自动审计：{{ nextScheduleTime }}</span>
          </template>
        </div>
      </div>
      <div class="header-actions">
        <button class="btn btn-ghost" :disabled="!hasData || exporting" @click="handleExportPdf">
          {{ exporting ? '导出中...' : '导出PDF' }}
        </button>
        <button class="btn btn-ghost" @click="router.push('/settings')">
          定时任务
        </button>
        <button
          class="btn btn-primary"
          :disabled="creating || !store.currentProject"
          @click="handleNewAudit"
        >
          {{ creating ? '审计中...' : '+ 新建审计' }}
        </button>
      </div>
    </div>

    <!-- Score Cards -->
    <div v-if="hasData" class="score-row">
      <ScoreCard
        v-for="card in scoreCards"
        :key="card.label"
        :label="card.label"
        :value="card.value"
        :suffix="card.suffix"
        :status="card.status"
        :change="card.change"
        :change-dir="card.changeDir"
        :benchmark="card.benchmark"
        :alert-badge="card.alertBadge"
      />
    </div>

    <!-- Empty State -->
    <div v-else class="empty-state">
      <div class="empty-icon">🔍</div>
      <h3>开始您的首次 AI 可见性审计</h3>
      <p>添加品牌和 Prompt 后，点击"新建审计"开始分析</p>
    </div>

    <!-- Platform Grid -->
    <PlatformGrid
      v-if="hasData && report"
      :platform-scores="report.platform_scores"
    />

    <!-- Trend + Competitor two-col layout -->
    <div v-if="store.currentProject" class="two-col">
      <TrendChart :project-id="store.currentProject.id" />
      <CompetitorTable :brands="store.brands" :report="report" />
    </div>

    <!-- Insights -->
    <InsightCard
      v-if="hasData && report"
      :insights="report.insights"
    />

    <!-- CTA Card -->
    <div v-if="ctaInfo && ctaInfo.score < 70" class="cta-card">
      <div>
        <h3>立即提升 {{ ctaInfo.platform }} 可见性（当前 {{ ctaInfo.score }} 分）</h3>
        <p>{{ ctaInfo.platform }} 是当前可见性最弱的平台。获取针对性的内容优化方案，提升品牌在 6 个 AI 平台上的推荐率。</p>
      </div>
      <button class="btn btn-cta" @click="router.push('/suggestions')">查看优化方案 →</button>
    </div>
  </div>
</template>

<style scoped>
.dashboard {
  max-width: 1200px;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 24px;
}

.header h1 {
  font-size: 18px;
  font-weight: 600;
}

.header-meta {
  font-size: 11px;
  color: var(--text-secondary);
  margin-top: 4px;
  display: flex;
  align-items: center;
  gap: 12px;
}

.freshness {
  display: flex;
  align-items: center;
  gap: 4px;
}

.dot-green {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
}

.meta-divider {
  color: var(--text-muted);
}

.schedule-hint {
  color: var(--text-secondary);
}

.header-actions {
  display: flex;
  gap: 8px;
}

.btn {
  padding: 7px 14px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  cursor: pointer;
  border: none;
  display: flex;
  align-items: center;
  gap: 5px;
  font-weight: 500;
  transition: all 0.15s ease;
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-primary {
  background: var(--accent);
  color: var(--bg-base);
}

.btn-primary:hover:not(:disabled) {
  background: #00e8bb;
}

.btn-ghost {
  background: transparent;
  color: var(--text-secondary);
  border: 1px solid var(--border);
}

.text-muted {
  color: var(--text-muted);
}

.score-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
  margin-bottom: 24px;
}

.empty-state {
  text-align: center;
  padding: 60px 20px;
  color: var(--text-muted);
}

.empty-icon {
  font-size: 48px;
  margin-bottom: 16px;
}

.empty-state h3 {
  font-size: 16px;
  color: var(--text-primary);
  margin-bottom: 8px;
}

.empty-state p {
  font-size: 13px;
}

.two-col {
  display: grid;
  grid-template-columns: 1.2fr 1fr;
  gap: 14px;
  margin-bottom: 24px;
}

.cta-card {
  background: linear-gradient(135deg, #0f3460 0%, #1a1a2e 50%, #0d1b2a 100%);
  border-radius: var(--radius-lg);
  padding: 24px;
  border: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
}

.cta-card h3 {
  font-size: 14px;
  margin-bottom: 6px;
  font-weight: 600;
}

.cta-card p {
  font-size: 11px;
  color: var(--text-secondary);
  line-height: 1.6;
  max-width: 480px;
}

.btn-cta {
  background: var(--accent);
  color: var(--bg-base);
  white-space: nowrap;
}

.btn-cta:hover {
  background: #00e8bb;
}

@media (max-width: 768px) {
  .score-row {
    grid-template-columns: repeat(2, 1fr);
  }
  .header {
    flex-direction: column;
    gap: 16px;
  }
  .cta-card {
    flex-direction: column;
    text-align: center;
  }
}
</style>
