<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useProjectStore } from '../../stores/project'
import { addPrompt, deletePrompt, generatePrompts } from '../../api/client'
import LoadingSkeleton from '../../components/common/LoadingSkeleton.vue'
import EmptyState from '../../components/common/EmptyState.vue'

const store = useProjectStore()
const newPromptText = ref('')
const newPromptCategory = ref('recommend')
const generating = ref(false)
const categories = [
  { value: 'recommend', label: '推荐类' },
  { value: 'compare', label: '对比类' },
  { value: 'evaluate', label: '评测类' },
  { value: 'scenario', label: '场景类' },
  { value: 'problem_solution', label: '问题解决类' },
  { value: 'alternative_finding', label: '替代方案类' },
  { value: 'decision_help', label: '决策辅助类' },
  { value: 'regret_avoidance', label: '避坑类' },
  { value: 'performance_specs', label: '参数性能类' },
]

async function handleAdd() {
  if (!newPromptText.value.trim() || !store.currentProject) return
  try {
    await addPrompt(store.currentProject.id, {
      text: newPromptText.value.trim(),
      category: newPromptCategory.value,
    })
    newPromptText.value = ''
    await store.fetchPrompts(store.currentProject.id)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '添加失败')
  }
}

async function handleDelete(promptId: number) {
  if (!store.currentProject) return
  try {
    await deletePrompt(store.currentProject.id, promptId)
    await store.fetchPrompts(store.currentProject.id)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '删除失败')
  }
}

async function handleAutoGenerate() {
  if (!store.currentProject || generating.value) return
  generating.value = true
  try {
    await generatePrompts(store.currentProject.id, 10, store.currentProject.product_category)
    await store.fetchPrompts(store.currentProject.id)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || 'AI生成失败')
  } finally {
    generating.value = false
  }
}

onMounted(() => {
  if (!store.currentProject) store.fetchProjects()
})
</script>

<template>
  <div>
    <div class="header">
      <h1>Prompt 管理</h1>
      <button
        class="btn btn-primary"
        :disabled="generating || !store.currentProject"
        @click="handleAutoGenerate"
      >
        {{ generating ? 'AI生成中...' : '+ AI生成Prompt' }}
      </button>
    </div>

    <!-- Add Prompt Form -->
    <div class="add-form">
      <input
        v-model="newPromptText"
        class="input flex-1"
        placeholder="输入 Prompt，如：推荐一款好的重疾险"
        @keyup.enter="handleAdd"
      />
      <select v-model="newPromptCategory" class="input select-input" style="width: 120px">
        <option v-for="cat in categories" :key="cat.value" :value="cat.value">{{ cat.label }}</option>
      </select>
      <button class="btn btn-primary" :disabled="!newPromptText.trim()" @click="handleAdd">
        添加
      </button>
    </div>

    <!-- Prompt List -->
    <LoadingSkeleton v-if="store.prompts.length === 0 && !store.currentProject" variant="list" :count="5" />
    <div v-else class="prompt-list">
      <div v-for="prompt in store.prompts" :key="prompt.id" class="prompt-item">
        <div class="prompt-content">
          <span class="prompt-text">{{ prompt.text }}</span>
          <span class="prompt-tag">{{ categories.find(c => c.value === prompt.category)?.label || prompt.category }}</span>
        </div>
        <button class="btn-delete" @click="handleDelete(prompt.id)">删除</button>
      </div>
      <EmptyState
        v-if="store.prompts.length === 0"
        icon="🔍"
        title="暂无 Prompt"
        description="请添加 Prompt 或使用 AI 自动生成"
      />
    </div>
  </div>
</template>

<style scoped>
.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--space-6);
}
.header h1 { font-size: var(--text-xl); font-weight: 600; }

.add-form {
  display: flex;
  gap: var(--space-2);
  margin-bottom: var(--space-4);
}
.flex-1 { flex: 1; }

.input {
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 5px 8px;
  font-size: var(--text-base);
  color: var(--text-primary);
  outline: none;
  height: 30px;
  transition: border-color var(--duration-fast);
}
.input:focus { border-color: var(--accent); }
.input::placeholder { color: var(--text-muted); }
.select-input {
  cursor: pointer;
  appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2371717a' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 10px center;
  padding-right: 28px;
}

.prompt-list {
  background: var(--bg-card);
  border-radius: var(--radius-md);
  border: 1px solid var(--border-light);
}
.prompt-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px var(--space-3);
  border-bottom: 1px solid var(--border-light);
}
.prompt-item:last-child { border-bottom: none; }
.prompt-item:hover { background: var(--bg-hover); }

.prompt-content {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex: 1;
}
.prompt-text { font-size: var(--text-base); color: var(--text-primary); }

.prompt-tag {
  font-size: 9px;
  padding: 1px 6px;
  border-radius: 10px;
  background: var(--accent-dim);
  color: var(--accent);
  font-weight: 600;
}

.btn-delete {
  background: none;
  border: none;
  color: var(--text-muted);
  font-size: var(--text-xs);
  cursor: pointer;
  padding: var(--space-1) var(--space-2);
  border-radius: var(--radius-sm);
  transition: all var(--duration-fast);
}
.btn-delete:hover { color: var(--status-bad); background: rgba(239, 68, 68, 0.1); }
</style>
