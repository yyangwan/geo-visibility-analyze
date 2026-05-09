<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useProjectStore } from '../../stores/project'
import { createProject, addBrand } from '../../api/client'

const store = useProjectStore()
const newProjectName = ref('')
const newProjectIndustry = ref('insurance')
const newBrandName = ref('')
const newBrandAliases = ref('')
const newBrandIsCompetitor = ref(false)

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
  })
  newProjectName.value = ''
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

onMounted(() => {
  if (!store.projects.length) store.fetchProjects()
})
</script>

<template>
  <div>
    <div class="header">
      <h1>平台配置</h1>
    </div>

    <!-- Project Management -->
    <div class="section">
      <h2>项目管理</h2>
      <div class="form-row">
        <el-select v-model="selectedProjectId" placeholder="选择项目" style="width: 200px">
          <el-option
            v-for="p in store.projects"
            :key="p.id"
            :label="p.name"
            :value="p.id"
          />
        </el-select>
        <el-input v-model="newProjectName" placeholder="新项目名称" style="width: 200px" />
        <el-input v-model="newProjectIndustry" placeholder="行业" style="width: 120px" />
        <el-button type="primary" @click="handleCreateProject">创建项目</el-button>
      </div>
    </div>

    <!-- Brand Management -->
    <div class="section">
      <h2>品牌管理</h2>
      <div class="form-row">
        <el-input v-model="newBrandName" placeholder="品牌名称" style="width: 160px" />
        <el-input v-model="newBrandAliases" placeholder="别名 (逗号分隔)" style="width: 200px" />
        <el-checkbox v-model="newBrandIsCompetitor">竞品</el-checkbox>
        <el-button type="primary" @click="handleAddBrand">添加品牌</el-button>
      </div>
      <div class="brand-list">
        <div v-for="brand in store.brands" :key="brand.id" class="brand-item">
          <span :class="{ competitor: brand.is_competitor }">{{ brand.name }}</span>
          <span v-if="brand.aliases?.length" class="aliases">({{ brand.aliases.join(', ') }})</span>
          <span class="tag" :class="brand.is_competitor ? 'tag-warn' : 'tag-good'">
            {{ brand.is_competitor ? '竞品' : '主品牌' }}
          </span>
        </div>
        <div v-if="!store.brands.length" class="empty">暂无品牌</div>
      </div>
    </div>

    <!-- Platform Status -->
    <div class="section">
      <h2>平台状态</h2>
      <div class="platform-list">
        <div v-for="p in ['DeepSeek', '通义千问', '豆包', 'Kimi', '文心一言', '腾讯元宝']" :key="p" class="platform-status">
          <span class="platform-name">{{ p }}</span>
          <span class="status-dot pending"></span>
          <span class="status-text">未配置</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.header { margin-bottom: 24px; }
.header h1 { font-size: 18px; font-weight: 600; }

.section {
  background: var(--bg-card);
  border-radius: var(--radius-md);
  padding: 20px;
  border: 1px solid var(--border-light);
  margin-bottom: 16px;
}

.section h2 {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 14px;
}

.form-row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 14px;
  flex-wrap: wrap;
}

.brand-list {
  margin-top: 8px;
}

.brand-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 0;
  border-bottom: 1px solid var(--border-light);
  font-size: 13px;
}

.brand-item:last-child { border-bottom: none; }

.competitor { color: var(--text-secondary); }

.aliases {
  color: var(--text-muted);
  font-size: 11px;
}

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
  padding: 20px;
  color: var(--text-muted);
  font-size: 13px;
}

.platform-list {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}

.platform-status {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  background: var(--bg-hover);
  border-radius: var(--radius-sm);
}

.platform-name {
  font-size: 12px;
  font-weight: 500;
}

.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}

.status-dot.pending { background: var(--text-muted); }
.status-text { font-size: 10px; color: var(--text-muted); }
</style>
