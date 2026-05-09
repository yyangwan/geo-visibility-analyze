<script setup lang="ts">
const props = defineProps<{
  insights: string[]
}>()

const iconConfig = [
  { emoji: '⚠️', bg: 'rgba(251,191,36,0.1)', type: 'warning' },
  { emoji: '📊', bg: 'rgba(0,212,170,0.1)', type: 'insight' },
  { emoji: '⚡', bg: 'rgba(76,201,240,0.1)', type: 'action' },
  { emoji: '💡', bg: 'rgba(0,212,170,0.1)', type: 'insight' },
  { emoji: '🎯', bg: 'rgba(76,201,240,0.1)', type: 'action' },
]
</script>

<template>
  <div>
    <div class="section-title">
      智能洞察 & 优化建议
      <span class="sub">基于 {{ insights.length }} 条分析 · 点击查看原始 AI 响应</span>
    </div>
    <div class="analysis-card">
      <div v-for="(insight, i) in insights" :key="i" class="analysis-item">
        <div class="analysis-icon" :class="iconConfig[i % iconConfig.length].type">
          {{ iconConfig[i % iconConfig.length].emoji }}
        </div>
        <div class="analysis-text">
          <div class="title">{{ insight }}</div>
          <div class="link">查看相关 Prompt 的 AI 原始响应 →</div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.section-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 12px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.sub {
  font-size: 11px;
  color: var(--text-muted);
  font-weight: 400;
}

.analysis-card {
  background: var(--bg-card);
  border-radius: var(--radius-md);
  padding: 18px;
  border: 1px solid var(--border-light);
  margin-bottom: 24px;
}

.analysis-item {
  display: flex;
  gap: 12px;
  padding: 12px 0;
  border-bottom: 1px solid var(--border-light);
}

.analysis-item:last-child {
  border-bottom: none;
}

.analysis-icon {
  width: 30px;
  height: 30px;
  border-radius: var(--radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.analysis-icon.warning {
  background: rgba(251,191,36,0.1);
}

.analysis-icon.insight {
  background: rgba(0,212,170,0.1);
}

.analysis-icon.action {
  background: rgba(76,201,240,0.1);
}

.analysis-text {
  flex: 1;
}

.title {
  font-size: 12px;
  font-weight: 600;
  line-height: 1.6;
  color: var(--text-primary);
}

.link {
  font-size: 10px;
  color: var(--accent);
  margin-top: 4px;
  cursor: pointer;
  transition: opacity 0.15s;
}

.link:hover {
  opacity: 0.8;
  text-decoration: underline;
}
</style>
