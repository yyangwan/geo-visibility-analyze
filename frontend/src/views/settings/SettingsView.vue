<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useProjectStore } from '../../stores/project'
import { createProject, addBrand, getPlatforms, updateProject } from '../../api/client'
import type { PlatformInfo } from '../../api/client'
import LoadingSkeleton from '../../components/common/LoadingSkeleton.vue'

const store = useProjectStore()
const newProjectName = ref('')
const newProjectIndustry = ref('insurance')
const newProductCategory = ref('')
const newBrandName = ref('')
const newBrandAliases = ref('')
const newBrandIsCompetitor = ref(false)
const platforms = ref<PlatformInfo[]>([])
const platformsLoading = ref(true)
const selectedProjectId = computed({
  get: () => store.currentProject?.id,
  set: (id: number | undefined) => {
    const p = store.projects.find(proj => proj.id === id)
    if (p) store.selectProject(p)
  },
})

async function handleCreateProject() {
  if (!newProjectName.value.trim()) return
  const { data } = await createProject({
    name: newProjectName.value.trim(),
    industry: newProjectIndustry.value,
    product_category: newProductCategory.value.trim(),
  })
  newProjectName.value = ''
  newProductCategory.value = ''
  await store.fetchProjects()
  await store.selectProject(data)
}

async function handleAddBrand() {
  if (!newBrandName.value.trim() || !store.currentProject) return
  await addBrand(store.currentProject.id, {
    name: newBrandName.value.trim(),
    aliases: newBrandAliases.value ? newBrandAliases.value.split(',').map(s => s.trim()) : [],
    is_competitor: newBrandIsCompetitor.value,
  })
  newBrandName.value = ''
  newBrandAliases.value = ''
  newBrandIsCompetitor.value = false
  await store.fetchBrands(store.currentProject.id)
}

const editCategory = ref<string | null>(null)
const savingCategory = ref(false)

async function handleSaveCategory() {
  if (!store.currentProject || editCategory.value === null) return
  savingCategory.value = true
  try {
    await updateProject(store.currentProject.id, { product_category: editCategory.value.trim() })
    await store.fetchProjects()
    const p = store.projects.find(proj => proj.id === store.currentProject?.id)
    if (p) await store.selectProject(p)
    editCategory.value = null
    ElMessage.success('品类已保存')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  }
  savingCategory.value = false
}

function startEditCategory() {
  editCategory.value = store.currentProject?.product_category || ''
}

async function fetchPlatforms() {
  platformsLoading.value = true
  try {
    const { data } = await getPlatforms()
    platforms.value = data
  } catch {
    platforms.value = []
  } finally {
    platformsLoading.value = false
  }
}

onMounted(() => {
  if (!store.projects.length) store.fetchProjects()
  fetchPlatforms()
})
</script>

<template>
  <div>
    <div class="header">
      <h1>平台配置</h1>
      <p class="header-desc">管理项目、品牌和查看平台连接状态</p>
    </div>

    <!-- Project Management -->
    <div class="card">
      <div class="card-title">项目管理</div>
      <div class="card-body">
        <div class="field-group" style="width: 200px">
          <label class="field-label">当前项目</label>
          <select v-model="selectedProjectId" class="input select-input">
            <option :value="undefined" disabled>选择项目</option>
            <option v-for="p in store.projects" :key="p.id" :value="p.id">{{ p.name }}</option>
          </select>
          <div v-if="store.currentProject" class="project-info">
            <span class="info-label">品类：</span>
            <span v-if="store.currentProject.product_category" class="info-value">{{ store.currentProject.product_category }}</span>
            <span v-else class="info-empty">未设置</span>
            <button class="btn-edit" @click="startEditCategory" title="编辑品类">编辑</button>
          </div>
          <div v-if="editCategory !== null" class="form-row" style="margin-top: 6px">
            <input
              v-model="editCategory"
              class="input"
              style="width: 200px"
              placeholder="如：百万医疗保险"
              @keyup.enter="handleSaveCategory"
            />
            <button class="btn btn-primary" :disabled="savingCategory" @click="handleSaveCategory">
              {{ savingCategory ? '保存中...' : '保存' }}
            </button>
            <button class="btn btn-ghost" @click="editCategory = null">取消</button>
          </div>
        </div>
        <div class="form-row" style="margin-top: 8px">
          <div class="field-group" style="width: 200px">
            <label class="field-label">项目名称</label>
            <input
              v-model="newProjectName"
              class="input"
              placeholder="输入新项目名称"
              @keyup.enter="handleCreateProject"
            />
          </div>
          <div class="field-group" style="width: 140px">
            <label class="field-label">行业</label>
            <select v-model="newProjectIndustry" class="input select-input">
              <option value="insurance">保险</option>
              <option value="finance">金融</option>
              <option value="realestate">房地产</option>
              <option value="education">教育</option>
              <option value="healthcare">医疗健康</option>
              <option value="ecommerce">电商</option>
              <option value="automotive">汽车</option>
              <option value="travel">旅游</option>
              <option value="food">餐饮</option>
              <option value="technology">科技</option>
            </select>
          </div>
          <div class="field-group" style="width: 200px">
            <label class="field-label">产品品类</label>
            <input v-model="newProductCategory" class="input" placeholder="如：百万医疗保险" />
          </div>
          <button class="btn btn-primary self-end" :disabled="!newProjectName.trim()" @click="handleCreateProject">
            创建项目
          </button>
        </div>
      </div>
    </div>

    <!-- Brand Management -->
    <div class="card">
      <div class="card-title">品牌管理</div>
      <div class="card-body">
        <div class="form-row">
          <div class="field-group" style="width: 180px">
            <label class="field-label">品牌名称</label>
            <input
              v-model="newBrandName"
              class="input"
              placeholder="品牌名称"
              @keyup.enter="handleAddBrand"
            />
          </div>
          <div class="field-group" style="width: 200px">
            <label class="field-label">别名（逗号分隔）</label>
            <input v-model="newBrandAliases" class="input" placeholder="别名 (可选)" />
          </div>
          <label class="checkbox-label self-end">
            <input type="checkbox" v-model="newBrandIsCompetitor" class="checkbox" />
            <span>竞品</span>
          </label>
          <button
            class="btn btn-primary self-end"
            :disabled="!newBrandName.trim() || !store.currentProject"
            @click="handleAddBrand"
          >
            添加品牌
          </button>
        </div>

        <div v-if="store.brands.length" class="brand-list">
          <div v-for="brand in store.brands" :key="brand.id" class="brand-item">
            <span class="brand-name" :class="{ competitor: brand.is_competitor }">{{ brand.name }}</span>
            <span v-if="brand.aliases?.length" class="brand-aliases">({{ brand.aliases.join(', ') }})</span>
            <span class="tag" :class="brand.is_competitor ? 'tag-warn' : 'tag-good'">
              {{ brand.is_competitor ? '竞品' : '主品牌' }}
            </span>
          </div>
        </div>
        <div v-else class="empty">当前项目暂无品牌，请先添加</div>
      </div>
    </div>

    <!-- Platform Status -->
    <div class="card">
      <div class="card-title">平台状态</div>
      <div class="card-body">
        <LoadingSkeleton v-if="platformsLoading" variant="card" :count="3" />
        <div v-else-if="platforms.length" class="platform-grid">
          <div v-for="p in platforms" :key="p.key" class="platform-card" :class="{ configured: p.configured }">
            <span class="dot" :class="p.configured ? 'dot-ok' : 'dot-muted'"></span>
            <div class="platform-info">
              <div class="platform-label">{{ p.label }}</div>
              <div class="platform-status-text" :class="p.configured ? 'text-ok' : 'text-muted'">
                {{ p.configured ? '已配置' : '未配置' }}
              </div>
            </div>
          </div>
        </div>
        <div v-else class="empty">暂无平台数据</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.header {
  margin-bottom: var(--space-6);
}
.header h1 {
  font-size: var(--text-xl);
  font-weight: 600;
}
.header-desc {
  font-size: var(--text-sm);
  color: var(--text-muted);
  margin-top: var(--space-1);
}

/* Card */
.card {
  background: var(--bg-card);
  border-radius: var(--radius-md);
  border: 1px solid var(--border-light);
  margin-bottom: var(--space-4);
}
.card-title {
  font-size: var(--text-sm);
  font-weight: 600;
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--border-light);
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.card-body {
  padding: var(--space-3);
}

/* Form */
.form-row {
  display: flex;
  gap: var(--space-2);
  align-items: flex-end;
  flex-wrap: wrap;
}
.field-group {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.field-label {
  font-size: var(--text-xs);
  font-weight: 500;
  color: var(--text-muted);
}
.flex-1 { flex: 1; min-width: 100px; }
.self-end { align-self: flex-end; }

.input {
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 5px 8px;
  font-size: var(--text-base);
  color: var(--text-primary);
  outline: none;
  transition: border-color var(--duration-fast);
  height: 30px;
}
.input:focus {
  border-color: var(--accent);
}
.input::placeholder {
  color: var(--text-muted);
}
.select-input {
  cursor: pointer;
  appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2371717a' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 10px center;
  padding-right: 28px;
}

.checkbox-label {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  font-size: var(--text-sm);
  color: var(--text-secondary);
  cursor: pointer;
  user-select: none;
}
.checkbox {
  accent-color: var(--accent);
  width: 14px;
  height: 14px;
}

/* Brand list */
.brand-list {
  margin-top: var(--space-3);
  border-top: 1px solid var(--border-light);
  padding-top: var(--space-2);
}
.brand-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 0;
  border-bottom: 1px solid var(--border-light);
  font-size: var(--text-sm);
}
.brand-item:last-child { border-bottom: none; }
.brand-item:hover { background: var(--bg-hover); margin: 0 -4px; padding: 5px 4px; border-radius: var(--radius-sm); }

.brand-name { color: var(--text-primary); }
.brand-name.competitor { color: var(--text-secondary); }
.brand-aliases { color: var(--text-muted); font-size: var(--text-xs); }

.tag {
  font-size: 9px;
  padding: 1px 6px;
  border-radius: 10px;
  font-weight: 600;
  margin-left: auto;
}
.tag-good { background: rgba(0, 212, 170, 0.12); color: var(--status-good); }
.tag-warn { background: rgba(251, 191, 36, 0.12); color: var(--status-warn); }

.empty {
  text-align: center;
  padding: var(--space-6) var(--space-4);
  color: var(--text-muted);
  font-size: var(--text-sm);
}

/* Platform grid */
.platform-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: var(--space-2);
}
.platform-card {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: 8px 10px;
  background: var(--bg-hover);
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
  transition: border-color var(--duration-fast);
}
.platform-card.configured {
  border-color: rgba(0, 212, 170, 0.15);
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}
.dot-ok { background: var(--status-good); box-shadow: 0 0 6px rgba(0, 212, 170, 0.4); }
.dot-muted { background: var(--text-muted); }

.platform-info { display: flex; flex-direction: column; gap: 2px; }
.platform-label { font-size: var(--text-sm); font-weight: 500; color: var(--text-primary); }
.platform-status-text { font-size: var(--text-xs); }
.platform-status-text.text-ok { color: var(--status-good); }
.platform-status-text.text-muted { color: var(--text-muted); }

/* Project info */
.project-info {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: var(--text-sm);
  margin-top: 6px;
}
.info-label { color: var(--text-muted); font-size: var(--text-xs); }
.info-value { color: var(--accent); font-weight: 500; }
.info-empty { color: var(--text-muted); font-style: italic; }
.btn-edit {
  margin-left: 8px;
  padding: 1px 8px;
  border-radius: 4px;
  font-size: var(--text-xs);
  background: transparent;
  color: var(--text-secondary);
  border: 1px solid var(--border);
  cursor: pointer;
  transition: all var(--duration-fast);
}
.btn-edit:hover { color: var(--accent); border-color: var(--accent); }
.btn-ghost {
  padding: 5px 12px;
  border-radius: var(--radius-sm);
  font-size: var(--text-sm);
  background: transparent;
  color: var(--text-secondary);
  border: 1px solid var(--border);
  cursor: pointer;
}
.btn-ghost:hover { border-color: var(--text-secondary); }
</style>
