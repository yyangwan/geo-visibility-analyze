<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
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
import { PLATFORM_LABELS } from '../../constants/platforms'
import { formatDateTime } from '../../utils/date'
import LoadingSkeleton from '../../components/common/LoadingSkeleton.vue'
import ErrorState from '../../components/common/ErrorState.vue'
import EmptyState from '../../components/common/EmptyState.vue'
import AuditProgressCard from '../../components/common/AuditProgressCard.vue'
import OnboardingWizard from '../../components/common/OnboardingWizard.vue'
import ScoreCard from '../../components/dashboard/ScoreCard.vue'

const router = useRouter()
const store = useProjectStore()
const creating = ref(false)
const exporting = ref(false)
const activeAuditId = ref<number | null>(null)
const loading = ref(true)
const error = ref('')
const showOnboarding = ref(false)
const onboardingSkipped = ref(false)
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
  return {
    platform: PLATFORM_LABELS[worst[0]] || worst[0],
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
  } catch {
    ElMessage.error('PDF 导出失败')
  } finally {
    exporting.value = false
  }
}

async function handleNewAudit() {
  if (!store.currentProject) return
  creating.value = true
  try {
    const { data: audit } = await createAudit({ project_id: store.currentProject.id })
    activeAuditId.value = audit.id
    creating.value = false
  } catch (e: any) {
    creating.value = false
    ElMessage.error(e?.response?.data?.detail || '创建审计失败')
  }
}

async function onAuditComplete() {
  if (activeAuditId.value) {
    try {
      await generateReport(activeAuditId.value)
      await store.fetchReport(activeAuditId.value)
      ElMessage.success('审计完成，报告已生成')
    } catch {
      ElMessage.error('生成报告失败')
    }
    activeAuditId.value = null
    loadPreviousData()
  }
}

function onAuditError(msg: string) {
  activeAuditId.value = null
  ElMessage.error(msg)
}

function onOnboardingDone() {
  showOnboarding.value = false
  onboardingSkipped.value = true
  loadData()
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
  } catch { /* non-critical */ }
}

async function loadScheduleInfo() {
  try {
    const { data } = await getSchedules()
    const active = data.filter((s: any) => s.is_active)
    if (active.length > 0) {
      const cron = active[0].cron_expression
      const parts = cron.split(' ')
      if (parts.length >= 2) {
        nextScheduleTime.value = `每天 ${parts[1].padStart(2, '0')}:${parts[0].padStart(2, '0')}`
      }
    }
  } catch { /* non-critical */ }
}

async function loadData() {
  loading.value = true
  error.value = ''
  try {
    await store.fetchProjects()
    if (!store.projects.length && !onboardingSkipped.value) {
      showOnboarding.value = true
    } else if (store.currentProject) {
      try {
        const { data } = await getLatestReport(store.currentProject.id)
        store.report = data
      } catch { /* no report yet */ }
      loadPreviousData()
      loadScheduleInfo()
    }
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '加载数据失败'
  } finally {
    loading.value = false
  }
}

onMounted(loadData)

async function retryLoad() {
  loading.value = true
  error.value = ''
  try {
    await store.fetchProjects()
    if (store.currentProject) {
      const { data } = await getLatestReport(store.currentProject.id)
      store.report = data
      loadPreviousData()
      loadScheduleInfo()
    }
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '加载数据失败'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="dashboard">
    <!-- Onboarding Wizard -->
    <OnboardingWizard v-if="showOnboarding" @done="onOnboardingDone" />

    <!-- Loading -->
    <LoadingSkeleton v-else-if="loading" variant="card" :count="4" />

    <!-- Error -->
    <ErrorState v-else-if="error" :message="error" @retry="retryLoad" />

    <template v-else>
      <!-- Header -->
      <div class="header">
        <div>
          <h1>{{ store.currentProject?.name || '智见' }} · AI搜索可见性报告</h1>
          <div class="header-meta">
            <span v-if="store.currentProject?.product_category" class="category-tag">{{ store.currentProject.product_category }}</span>
            <span v-if="hasData" class="freshness">
              <span class="dot-green"></span>
              基于 {{ store.prompts.length }} 条 Prompt · {{ store.report?.platform_scores ? Object.keys(store.report.platform_scores).length : 0 }} 个平台
              <span class="meta-divider">|</span>
              审计时间 {{ formatDateTime(report?.created_at) }}
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

      <!-- Audit Progress -->
      <AuditProgressCard
        v-if="activeAuditId"
        :audit-id="activeAuditId"
        @complete="onAuditComplete"
        @error="onAuditError"
      />

      <!-- Workspace Layout -->
      <div v-if="hasData && report" class="workspace">
        <!-- Left: Dominant Score -->
        <div class="workspace-left">
          <div class="hero-score" :class="report.overall_score >= 70 ? 'hero-good' : report.overall_score >= 40 ? 'hero-warn' : 'hero-bad'">
            <div class="hero-number">{{ Math.round(report.overall_score) }}</div>
            <div class="hero-label">综合可见性评分</div>
          </div>
          <div class="hero-meta">
            <div class="meta-item" v-if="report.competitor_rank">
              <span class="meta-value">{{ '#' + report.competitor_rank }}</span>
              <span class="meta-label">竞品排名</span>
            </div>
            <div class="meta-item">
              <span class="meta-value">{{ Math.round(report.mention_rate * 100) }}%</span>
              <span class="meta-label">提及率</span>
            </div>
            <div class="meta-item" v-if="scoreCards[0]?.change">
              <span class="meta-value" :class="scoreCards[0].changeDir">{{ scoreCards[0].change }}</span>
              <span class="meta-label">变化</span>
            </div>
          </div>
          <!-- Platform Strip -->
          <div class="platform-strip">
            <div
              v-for="(score, platform) in report.platform_scores"
              :key="platform"
              class="strip-item"
              :class="score >= 70 ? 'strip-good' : score >= 50 ? 'strip-warn' : 'strip-bad'"
            >
              <div class="strip-name">{{ PLATFORM_LABELS[platform] || platform }}</div>
              <div class="strip-bar">
                <div class="strip-fill" :style="{ width: score + '%' }"></div>
              </div>
              <div class="strip-score">{{ Math.round(score) }}</div>
            </div>
          </div>
        </div>

        <!-- Right: KPI Rail + Actions -->
        <div class="workspace-right">
          <div class="kpi-rail">
            <ScoreCard
              v-for="card in scoreCards"
              :key="card.label"
              :label="card.label"
              :value="card.value"
              :suffix="card.suffix"
              :status="card.status"
              :change="card.change"
              :change-dir="card.changeDir"
              :alert-badge="card.alertBadge"
            />
          </div>

          <!-- Insights + Actions merged -->
          <div v-if="report.insights?.length" class="action-section">
            <div class="action-header">关键发现</div>
            <ul class="insight-list">
              <li v-for="(insight, i) in report.insights.slice(0, 4)" :key="i">{{ insight }}</li>
            </ul>
          </div>

          <!-- CTA -->
          <div v-if="ctaInfo && ctaInfo.score < 70" class="cta-inline">
            <span class="cta-text">{{ ctaInfo.platform }} 评分 {{ ctaInfo.score }}，建议优化</span>
            <button class="btn btn-cta" @click="router.push('/suggestions')">查看方案 →</button>
          </div>

          <!-- Nav links to dedicated pages -->
          <div class="quick-links">
            <button class="btn btn-ghost" @click="router.push('/trends')">趋势追踪 →</button>
            <button class="btn btn-ghost" @click="router.push('/competitors')">竞品对比 →</button>
          </div>
        </div>
      </div>

      <!-- Empty State -->
      <EmptyState
        v-else
        icon="🔍"
        title="开始您的首次 AI 可见性审计"
        description="添加品牌和 Prompt 后，点击「新建审计」开始分析"
        action-label="+ 新建审计"
        @action="handleNewAudit"
      />

    </template>
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
  display: flex;
  align-items: center;
  gap: 12px;
}

.category-tag {
  padding: 2px 10px;
  border-radius: 10px;
  font-size: 10px;
  font-weight: 600;
  background: var(--accent-dim);
  color: var(--accent);
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

.meta-divider { color: var(--text-muted); }
.schedule-hint { color: var(--text-secondary); }

.header-actions {
  display: flex;
  gap: 8px;
}

.text-muted { color: var(--text-muted); }

/* — Workspace Layout — */
.workspace {
  display: grid;
  grid-template-columns: 1fr 380px;
  gap: 20px;
}

.workspace-left {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.hero-score {
  background: var(--bg-card);
  border-radius: var(--radius-lg);
  padding: 28px 32px;
  border: 1px solid var(--border-light);
  text-align: center;
}

.hero-good { border-left: 4px solid var(--status-good); }
.hero-warn { border-left: 4px solid var(--status-warn); }
.hero-bad { border-left: 4px solid var(--status-bad); }

.hero-number {
  font-size: 64px;
  font-weight: 800;
  font-family: "Inter", monospace;
  line-height: 1;
}

.hero-good .hero-number { color: var(--status-good); }
.hero-warn .hero-number { color: var(--status-warn); }
.hero-bad .hero-number { color: var(--status-bad); }

.hero-label {
  font-size: var(--text-sm);
  color: var(--text-muted);
  margin-top: 6px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.hero-meta {
  display: flex;
  gap: 20px;
  justify-content: center;
}

.meta-item {
  text-align: center;
}

.meta-value {
  font-size: var(--text-xl);
  font-weight: 700;
  font-family: "Inter", monospace;
  display: block;
}

.meta-value.up { color: var(--status-good); }
.meta-value.down { color: var(--status-bad); }

.meta-label {
  font-size: var(--text-xs);
  color: var(--text-muted);
}

/* Platform Strip */
.platform-strip {
  background: var(--bg-card);
  border-radius: var(--radius-md);
  padding: 14px 16px;
  border: 1px solid var(--border-light);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.strip-item {
  display: grid;
  grid-template-columns: 90px 1fr 32px;
  align-items: center;
  gap: 10px;
}

.strip-name {
  font-size: var(--text-sm);
  font-weight: 500;
  color: var(--text-secondary);
}

.strip-bar {
  height: 6px;
  background: var(--border);
  border-radius: 3px;
  overflow: hidden;
}

.strip-good .strip-fill { background: var(--status-good); }
.strip-warn .strip-fill { background: var(--status-warn); }
.strip-bad .strip-fill { background: var(--status-bad); }

.strip-fill {
  height: 100%;
  border-radius: 3px;
  transition: width 0.4s ease;
}

.strip-score {
  font-size: var(--text-sm);
  font-weight: 700;
  font-family: "Inter", monospace;
  text-align: right;
}

.strip-good .strip-score { color: var(--status-good); }
.strip-warn .strip-score { color: var(--status-warn); }
.strip-bad .strip-score { color: var(--status-bad); }

/* Right Panel */
.workspace-right {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.kpi-rail {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.action-section {
  background: var(--bg-card);
  border-radius: var(--radius-md);
  padding: 14px 16px;
  border: 1px solid var(--border-light);
}

.action-header {
  font-size: var(--text-sm);
  font-weight: 600;
  margin-bottom: 10px;
}

.insight-list {
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.insight-list li {
  font-size: var(--text-xs);
  color: var(--text-secondary);
  padding-left: 14px;
  position: relative;
  line-height: 1.5;
}

.insight-list li::before {
  content: '•';
  position: absolute;
  left: 0;
  color: var(--accent);
}

.cta-inline {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  background: rgba(0, 212, 170, 0.06);
  border: 1px solid var(--accent-dim);
  border-radius: var(--radius-md);
}

.cta-text {
  font-size: var(--text-sm);
  color: var(--text-secondary);
}

.quick-links {
  display: flex;
  gap: 8px;
}

@media (max-width: 768px) {
  .workspace {
    grid-template-columns: 1fr;
  }
  .kpi-rail {
    grid-template-columns: 1fr 1fr;
  }
  .header {
    flex-direction: column;
    gap: 16px;
  }
  .hero-number {
    font-size: 48px;
  }
}
</style>
