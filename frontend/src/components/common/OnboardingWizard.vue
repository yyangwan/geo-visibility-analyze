<script setup lang="ts">
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
import {
  createProject,
  addBrand,
  generatePrompts,
  createAudit,
  generateReport,
} from '../../api/client'
import { useProjectStore } from '../../stores/project'
import AuditProgressCard from './AuditProgressCard.vue'

const store = useProjectStore()
const emit = defineEmits<{ done: [] }>()

const step = ref(0) // 0-4
const totalSteps = 5

// Step 1: Create project
const projectName = ref('')
const projectIndustry = ref('insurance')

// Step 2: Add brand
const brandName = ref('')
const brandAliases = ref('')

// Step 3: Add competitors
const competitorName = ref('')
const competitors = ref<string[]>([])

// Step 4: Generate prompts (loading state)
const generating = ref(false)

// Step 5: Run first audit
const activeAuditId = ref<number | null>(null)
const projectId = ref<number | null>(null)

const stepLabels = [
  '创建项目',
  '添加品牌',
  '添加竞品',
  '生成 Prompt',
  '首次审计',
]

const canNext = computed(() => {
  if (step.value === 0) return projectName.value.trim().length > 0
  if (step.value === 1) return brandName.value.trim().length > 0
  return true
})

async function handleNext() {
  if (step.value === 0) {
    // Create project
    try {
      const { data } = await createProject({
        name: projectName.value.trim(),
        industry: projectIndustry.value,
      })
      projectId.value = data.id
      await store.fetchProjects()
      const p = store.projects.find(proj => proj.id === data.id)
      if (p) await store.selectProject(p)
    } catch (e: any) {
      ElMessage.error(e?.response?.data?.detail || '创建项目失败')
      return
    }
  } else if (step.value === 1) {
    // Add brand
    if (!projectId.value) return
    try {
      await addBrand(projectId.value, {
        name: brandName.value.trim(),
        aliases: brandAliases.value ? brandAliases.value.split(',').map(s => s.trim()) : [],
        is_competitor: false,
      })
      await store.fetchBrands(projectId.value)
    } catch (e: any) {
      ElMessage.error(e?.response?.data?.detail || '添加品牌失败')
      return
    }
  } else if (step.value === 2) {
    // Add competitors
    if (!projectId.value) return
    for (const name of competitors.value) {
      try {
        await addBrand(projectId.value, { name, is_competitor: true })
      } catch { /* skip duplicates */ }
    }
    await store.fetchBrands(projectId.value)
  } else if (step.value === 3) {
    // Generate prompts
    if (!projectId.value) return
    generating.value = true
    try {
      await generatePrompts(projectId.value, 10)
      await store.fetchPrompts(projectId.value)
    } catch (e: any) {
      ElMessage.error(e?.response?.data?.detail || '生成 Prompt 失败')
      generating.value = false
      return
    }
    generating.value = false
  }

  step.value++
}

function addCompetitor() {
  const name = competitorName.value.trim()
  if (name && !competitors.value.includes(name)) {
    competitors.value.push(name)
    competitorName.value = ''
  }
}

function removeCompetitor(name: string) {
  competitors.value = competitors.value.filter(n => n !== name)
}

async function handleStartAudit() {
  if (!projectId.value) return
  try {
    const { data: audit } = await createAudit({ project_id: projectId.value })
    activeAuditId.value = audit.id
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '启动审计失败')
  }
}

async function onAuditComplete() {
  if (activeAuditId.value && projectId.value) {
    try {
      await generateReport(activeAuditId.value)
      await store.fetchReport(activeAuditId.value)
      ElMessage.success('审计完成！您的 AI 可见性报告已就绪。')
    } catch {
      ElMessage.error('生成报告失败')
    }
    emit('done')
  }
}

function skipWizard() {
  emit('done')
}
</script>

<template>
  <div class="wizard-overlay" role="dialog" aria-modal="true" aria-label="新手指引">
    <div class="wizard-card">
      <!-- Progress bar -->
      <div class="wizard-progress">
        <div
          v-for="i in totalSteps"
          :key="i"
          class="progress-dot"
          :class="{ active: i - 1 <= step, current: i - 1 === step }"
        />
      </div>

      <!-- Step content -->
      <div class="wizard-body">
        <h2>{{ stepLabels[step] }}</h2>
        <p class="step-desc">
          <template v-if="step === 0">输入您的项目名称和所属行业，开始追踪品牌在 AI 平台上的可见性。</template>
          <template v-else-if="step === 1">添加您要监测的主品牌名称，可以包含别名（如英文名）。</template>
          <template v-else-if="step === 2">添加主要竞争对手，系统将对比品牌与竞品在 AI 回答中的表现。</template>
          <template v-else-if="step === 3">AI 将根据您的行业和品牌信息自动生成测试 Prompt。</template>
          <template v-else>系统将在 6 个 AI 平台上查询您的品牌可见性。</template>
        </p>

        <!-- Step 0: Create project -->
        <div v-if="step === 0" class="form-group">
          <label>项目名称</label>
          <input v-model="projectName" placeholder="如：XX保险AI可见性监测" @keyup.enter="handleNext" />
          <label style="margin-top: 12px">行业</label>
          <select v-model="projectIndustry">
            <option value="insurance">保险</option>
            <option value="finance">金融</option>
            <option value="healthcare">医疗健康</option>
            <option value="education">教育</option>
            <option value="ecommerce">电商</option>
            <option value="realestate">房地产</option>
            <option value="technology">科技</option>
            <option value="other">其他</option>
          </select>
        </div>

        <!-- Step 1: Add brand -->
        <div v-if="step === 1" class="form-group">
          <label>品牌名称</label>
          <input v-model="brandName" placeholder="如：XX保险" @keyup.enter="handleNext" />
          <label style="margin-top: 12px">品牌别名（逗号分隔）</label>
          <input v-model="brandAliases" placeholder="如：XX Insurance, xx-insurance" @keyup.enter="handleNext" />
        </div>

        <!-- Step 2: Add competitors -->
        <div v-if="step === 2" class="form-group">
          <div class="input-row">
            <input v-model="competitorName" placeholder="竞品名称" @keyup.enter="addCompetitor" />
            <button class="btn btn-ghost" @click="addCompetitor" :disabled="!competitorName.trim()">添加</button>
          </div>
          <div v-if="competitors.length" class="tag-list">
            <span v-for="c in competitors" :key="c" class="tag-item">
              {{ c }}
              <button class="tag-remove" @click="removeCompetitor(c)">&times;</button>
            </span>
          </div>
          <p v-else class="hint">至少添加一个竞品以获得对比数据，也可以跳过此步骤</p>
        </div>

        <!-- Step 3: Generate prompts -->
        <div v-if="step === 3" class="form-group centered">
          <div v-if="generating" class="generating">
            <span class="spinner"></span>
            AI 正在生成 Prompt...
          </div>
          <p v-else>点击下一步，AI 将为您自动生成 10 条与行业相关的测试 Prompt。</p>
        </div>

        <!-- Step 4: Run first audit -->
        <div v-if="step === 4" class="form-group centered">
          <AuditProgressCard
            v-if="activeAuditId"
            :audit-id="activeAuditId"
            @complete="onAuditComplete"
            @error="(msg: string) => ElMessage.error(msg)"
          />
          <template v-else>
            <p>一切就绪！点击下方按钮启动首次 AI 可见性审计。</p>
            <button class="btn btn-primary btn-lg" @click="handleStartAudit">
              开始首次审计
            </button>
          </template>
        </div>
      </div>

      <!-- Footer -->
      <div class="wizard-footer">
        <button class="btn btn-ghost" @click="skipWizard">
          {{ step === 4 && !activeAuditId ? '跳过，稍后设置' : '跳过向导' }}
        </button>
        <button
          v-if="step < 4 || (step === 4 && !activeAuditId)"
          class="btn btn-primary"
          :disabled="!canNext || generating"
          @click="handleNext"
        >
          {{ generating ? '生成中...' : step === 3 ? '生成 Prompt' : step === 4 ? '跳过' : '下一步' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.wizard-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.7);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 200;
  padding: 20px;
}

.wizard-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  width: 100%;
  max-width: 520px;
  display: flex;
  flex-direction: column;
  max-height: 90vh;
}

.wizard-progress {
  display: flex;
  justify-content: center;
  gap: 8px;
  padding: 20px 20px 0;
}

.progress-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--border);
  transition: all var(--duration-fast);
}

.progress-dot.active {
  background: var(--accent-dim);
}

.progress-dot.current {
  background: var(--accent);
  width: 24px;
  border-radius: 4px;
}

.wizard-body {
  padding: 24px 28px;
  flex: 1;
  overflow-y: auto;
}

.wizard-body h2 {
  font-size: var(--text-xl);
  font-weight: 700;
  margin-bottom: 6px;
}

.step-desc {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  margin-bottom: 20px;
  line-height: 1.6;
}

.form-group label {
  display: block;
  font-size: var(--text-xs);
  color: var(--text-secondary);
  margin-bottom: 6px;
  font-weight: 500;
}

.form-group input,
.form-group select {
  width: 100%;
  padding: 10px 12px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--bg-input);
  color: var(--text-primary);
  font-size: var(--text-base);
  outline: none;
  transition: border-color var(--duration-fast);
  box-sizing: border-box;
}

.form-group input:focus,
.form-group select:focus {
  border-color: var(--accent);
}

.form-group.centered {
  text-align: center;
  padding: 12px 0;
}

.input-row {
  display: flex;
  gap: 8px;
}

.input-row input {
  flex: 1;
}

.tag-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 12px;
}

.tag-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  background: var(--accent-dim);
  color: var(--accent);
  border-radius: 14px;
  font-size: var(--text-xs);
  font-weight: 500;
}

.tag-remove {
  background: none;
  border: none;
  color: var(--accent);
  cursor: pointer;
  font-size: 14px;
  padding: 0;
  line-height: 1;
  opacity: 0.6;
}

.tag-remove:hover {
  opacity: 1;
}

.hint {
  font-size: var(--text-xs);
  color: var(--text-muted);
  margin-top: 8px;
}

.generating {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: var(--text-secondary);
  font-size: var(--text-sm);
}

.spinner {
  width: 18px;
  height: 18px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

.btn-lg {
  padding: 12px 28px;
  font-size: var(--text-md);
  margin-top: 12px;
}

.wizard-footer {
  display: flex;
  justify-content: space-between;
  padding: 16px 28px;
  border-top: 1px solid var(--border-light);
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

@media (max-width: 640px) {
  .wizard-card {
    max-width: none;
    border-radius: 0;
    max-height: 100vh;
  }
  .wizard-overlay {
    padding: 0;
  }
}
</style>
