<script setup lang="ts">
import { PLATFORM_LABELS } from '../../constants/platforms'

const props = defineProps<{
  platformScores: Record<string, number>
  mentionRates?: Record<string, number>
}>()

const platformNames = PLATFORM_LABELS

function getStatus(score: number): 'good' | 'warn' | 'bad' {
  if (score >= 70) return 'good'
  if (score >= 50) return 'warn'
  return 'bad'
}

function getStatusLabel(score: number): string {
  if (score >= 70) return '优秀'
  if (score >= 50) return '一般'
  return '待优化'
}
</script>

<template>
  <div>
    <div class="section-title">
      各平台可见性详情
      <span class="sub">点击平台卡片查看 Prompt 级别详情</span>
    </div>
    <div class="platform-grid">
      <div
        v-for="(score, platform) in platformScores"
        :key="platform"
        class="platform-card"
        :class="{ 'card-alert': getStatus(score) === 'bad' }"
      >
        <div class="platform-header">
          <span class="platform-name" :class="{ 'name-alert': getStatus(score) === 'bad' }">
            {{ platformNames[platform] || platform }}
          </span>
          <span class="platform-score" :class="getStatus(score)">{{ score }}</span>
        </div>
        <div class="platform-bar">
          <div
            class="platform-bar-fill"
            :style="{
              width: score + '%',
              background: getStatus(score) === 'good' ? 'var(--status-good)' : getStatus(score) === 'warn' ? 'var(--status-warn)' : 'var(--status-bad)',
            }"
          />
        </div>
        <div class="platform-detail">
          <span>{{ mentionRates && mentionRates[platform] != null ? Math.round(mentionRates[platform] * 100) + '%' : '-' }}</span>
          <span class="tag" :class="'tag-' + getStatus(score)">
            {{ getStatusLabel(score) }}
          </span>
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

.platform-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  margin-bottom: 24px;
}

.platform-card {
  background: var(--bg-card);
  border-radius: var(--radius-md);
  padding: 14px 16px;
  border: 1px solid var(--border-light);
  cursor: pointer;
  transition: all 0.15s ease;
}

.platform-card:hover {
  border-color: var(--accent);
}

.card-alert {
  border-color: rgba(239, 68, 68, 0.3);
}

.platform-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
}

.platform-name {
  font-size: 12px;
  font-weight: 600;
}

.name-alert {
  color: var(--status-bad);
}

.platform-score {
  font-size: 20px;
  font-weight: 700;
  font-family: "Inter", monospace;
}

.platform-score.good { color: var(--status-good); }
.platform-score.warn { color: var(--status-warn); }
.platform-score.bad { color: var(--status-bad); }

.platform-bar {
  height: 4px;
  background: var(--border);
  border-radius: 2px;
  overflow: hidden;
  margin-bottom: 8px;
}

.platform-bar-fill {
  height: 100%;
  border-radius: 2px;
  transition: width 0.5s ease;
}

.platform-detail {
  font-size: 10px;
  color: var(--text-muted);
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.tag {
  font-size: 9px;
  padding: 1px 6px;
  border-radius: 10px;
  font-weight: 600;
}

.tag-good { background: rgba(0, 212, 170, 0.12); color: var(--status-good); }
.tag-warn { background: rgba(251, 191, 36, 0.12); color: var(--status-warn); }
.tag-bad { background: rgba(239, 68, 68, 0.12); color: var(--status-bad); }

@media (max-width: 768px) {
  .platform-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
</style>
