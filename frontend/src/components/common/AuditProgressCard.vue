<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { createAuditEventSource, type AuditProgressEvent } from '../../api/client'
import { PLATFORM_LABELS } from '../../constants/platforms'

const props = defineProps<{
  auditId: number
}>()

const emit = defineEmits<{
  complete: []
  error: [message: string]
}>()

type PlatformStatus = 'pending' | 'running' | 'done' | 'error'

const platformStatuses = ref<Record<string, PlatformStatus>>({})
const errorMessage = ref('')
let eventSource: EventSource | null = null

const totalPlatforms = computed(() => Object.keys(platformStatuses.value).length)
const completedPlatforms = computed(() =>
  Object.values(platformStatuses.value).filter(s => s === 'done').length
)
const progressLabel = computed(() => `${completedPlatforms.value}/${totalPlatforms.value}`)

onMounted(() => {
  eventSource = createAuditEventSource(props.auditId)

  eventSource.addEventListener('platform_start', (e) => {
    const data = JSON.parse(e.data) as AuditProgressEvent
    if (data.platform) {
      if (!platformStatuses.value[data.platform]) {
        platformStatuses.value[data.platform] = 'running'
      } else {
        platformStatuses.value[data.platform] = 'running'
      }
    }
  })

  eventSource.addEventListener('platform_done', (e) => {
    const data = JSON.parse(e.data) as AuditProgressEvent
    if (data.platform) {
      platformStatuses.value[data.platform] = 'done'
    }
  })

  eventSource.addEventListener('platform_error', (e) => {
    const data = JSON.parse(e.data) as AuditProgressEvent
    if (data.platform) {
      platformStatuses.value[data.platform] = 'error'
    }
  })

  eventSource.addEventListener('audit_done', () => {
    cleanup()
    emit('complete')
  })

  eventSource.addEventListener('audit_failed', (e) => {
    const data = JSON.parse(e.data) as AuditProgressEvent
    errorMessage.value = data.error || '审计失败'
    cleanup()
    emit('error', errorMessage.value)
  })

  eventSource.onerror = () => {
    // Connection lost — could be audit completed before we subscribed
    cleanup()
  }
})

onUnmounted(() => {
  cleanup()
})

function cleanup() {
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }
}
</script>

<template>
  <div class="progress-card">
    <div class="progress-header">
      <span class="progress-title">审计进行中</span>
      <span class="progress-count">{{ progressLabel }} 平台完成</span>
    </div>
    <div class="platform-list">
      <div
        v-for="(status, platform) in platformStatuses"
        :key="platform"
        class="platform-row"
      >
        <span class="platform-icon" :class="status">
          <template v-if="status === 'done'">&#10003;</template>
          <template v-else-if="status === 'error'">&#10007;</template>
          <template v-else-if="status === 'running'">&#9679;</template>
        </span>
        <span class="platform-name">{{ PLATFORM_LABELS[platform] || platform }}</span>
      </div>
    </div>
    <div v-if="errorMessage" class="error-msg">{{ errorMessage }}</div>
  </div>
</template>

<style scoped>
.progress-card {
  background: var(--bg-card);
  border: 1px solid var(--accent-dim);
  border-radius: var(--radius-lg);
  padding: 18px 20px;
  margin-bottom: 20px;
}

.progress-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
}

.progress-title {
  font-size: var(--text-md);
  font-weight: 600;
  color: var(--accent);
}

.progress-count {
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.platform-list {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 16px;
}

.platform-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.platform-icon {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 9px;
  font-weight: 700;
  flex-shrink: 0;
}

.platform-icon.pending {
  background: var(--bg-hover);
  color: var(--text-muted);
}

.platform-icon.running {
  background: var(--accent-dim);
  color: var(--accent);
  animation: pulse 1.2s ease-in-out infinite;
}

.platform-icon.done {
  background: rgba(0, 212, 170, 0.2);
  color: var(--status-good);
}

.platform-icon.error {
  background: rgba(239, 68, 68, 0.2);
  color: var(--status-bad);
}

.platform-name {
  font-size: var(--text-sm);
  color: var(--text-secondary);
}

.error-msg {
  margin-top: 10px;
  font-size: var(--text-xs);
  color: var(--status-bad);
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}
</style>
