<script setup lang="ts">
defineProps<{
  icon?: string
  title: string
  description?: string
  actionLabel?: string
}>()

defineEmits<{
  action: []
}>()
</script>

<template>
  <div class="empty-state">
    <div v-if="icon" class="empty-icon">{{ icon }}</div>
    <h3>{{ title }}</h3>
    <p v-if="description">{{ description }}</p>
    <button v-if="actionLabel" class="btn btn-primary action-btn" @click="$emit('action')">
      {{ actionLabel }}
    </button>
  </div>
</template>

<style scoped>
.empty-state {
  text-align: center;
  padding: 48px 20px;
  color: var(--text-muted);
}

.empty-icon {
  font-size: 40px;
  margin-bottom: 14px;
  opacity: 0.6;
}

.empty-state h3 {
  font-size: 15px;
  color: var(--text-primary);
  margin-bottom: 6px;
  font-weight: 600;
}

.empty-state p {
  font-size: 13px;
  line-height: 1.5;
  margin-bottom: 18px;
  max-width: 360px;
  margin-left: auto;
  margin-right: auto;
}

.action-btn {
  font-size: 12px;
}
</style>
