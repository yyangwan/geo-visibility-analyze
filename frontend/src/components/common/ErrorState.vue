<script setup lang="ts">
defineProps<{
  message?: string
  retryLabel?: string
}>()

defineEmits<{
  retry: []
}>()
</script>

<template>
  <div class="error-state">
    <div class="error-icon">!</div>
    <h3>加载失败</h3>
    <p>{{ message || '数据加载出错，请稍后重试' }}</p>
    <button class="btn btn-ghost retry-btn" @click="$emit('retry')">
      {{ retryLabel || '重试' }}
    </button>
  </div>
</template>

<style scoped>
.error-state {
  text-align: center;
  padding: 48px 20px;
  color: var(--text-muted);
}

.error-icon {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  background: rgba(239, 68, 68, 0.12);
  color: var(--status-bad);
  font-size: 16px;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 14px;
}

.error-state h3 {
  font-size: 14px;
  color: var(--text-primary);
  margin-bottom: 6px;
}

.error-state p {
  font-size: 12px;
  margin-bottom: 16px;
  max-width: 320px;
  margin-left: auto;
  margin-right: auto;
}

.retry-btn {
  font-size: 12px;
}
</style>
