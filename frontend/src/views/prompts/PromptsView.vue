<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useProjectStore } from '../../stores/project'
import { addPrompt, deletePrompt, generatePrompts } from '../../api/client'

const store = useProjectStore()
const newPromptText = ref('')
const newPromptCategory = ref('recommend')
const generating = ref(false)
const categories = [
  { value: 'recommend', label: '推荐类' },
  { value: 'compare', label: '对比类' },
  { value: 'evaluate', label: '评测类' },
  { value: 'scenario', label: '场景类' },
]

async function handleAdd() {
  if (!newPromptText.value.trim() || !store.currentProject) return
  await addPrompt(store.currentProject.id, {
    text: newPromptText.value.trim(),
    category: newPromptCategory.value,
  })
  newPromptText.value = ''
  await store.fetchPrompts(store.currentProject.id)
}

async function handleDelete(promptId: number) {
  if (!store.currentProject) return
  await deletePrompt(store.currentProject.id, promptId)
  await store.fetchPrompts(store.currentProject.id)
}

async function handleAutoGenerate() {
  if (!store.currentProject || generating.value) return
  generating.value = true
  try {
    await generatePrompts(store.currentProject.id, 10)
    await store.fetchPrompts(store.currentProject.id)
  } catch (e: any) {
    alert(e?.response?.data?.detail || 'AI生成失败，请检查平台配置')
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
      <el-input
        v-model="newPromptText"
        placeholder="输入 Prompt，如：推荐一款好的重疾险"
        @keyup.enter="handleAdd"
        size="default"
      />
      <el-select v-model="newPromptCategory" size="default" style="width: 120px">
        <el-option
          v-for="cat in categories"
          :key="cat.value"
          :label="cat.label"
          :value="cat.value"
        />
      </el-select>
      <el-button type="primary" @click="handleAdd" :disabled="!newPromptText.trim()">
        添加
      </el-button>
    </div>

    <!-- Prompt List -->
    <div class="prompt-list">
      <div v-for="prompt in store.prompts" :key="prompt.id" class="prompt-item">
        <div class="prompt-content">
          <span class="prompt-text">{{ prompt.text }}</span>
          <span class="prompt-tag">{{ categories.find(c => c.value === prompt.category)?.label || prompt.category }}</span>
        </div>
        <el-button type="danger" text size="small" @click="handleDelete(prompt.id)">
          删除
        </el-button>
      </div>
      <div v-if="store.prompts.length === 0" class="empty">
        暂无 Prompt，请添加
      </div>
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

.add-form {
  display: flex;
  gap: 8px;
  margin-bottom: 20px;
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
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-light);
}

.prompt-item:last-child { border-bottom: none; }
.prompt-item:hover { background: var(--bg-hover); }

.prompt-content {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 1;
}

.prompt-text {
  font-size: 13px;
  color: var(--text-primary);
}

.prompt-tag {
  font-size: 9px;
  padding: 1px 6px;
  border-radius: 10px;
  background: var(--accent-dim);
  color: var(--accent);
  font-weight: 600;
}

.empty {
  text-align: center;
  padding: 40px;
  color: var(--text-muted);
  font-size: 13px;
}
</style>
