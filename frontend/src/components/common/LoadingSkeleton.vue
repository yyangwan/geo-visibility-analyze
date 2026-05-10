<script setup lang="ts">
defineProps<{
  variant?: 'card' | 'table' | 'chart' | 'list'
  count?: number
}>()
</script>

<template>
  <div class="skeleton-wrapper">
    <template v-if="variant === 'card'">
      <div v-for="i in (count ?? 4)" :key="i" class="skeleton-card">
        <div class="skeleton-line w-24 h-3 mb" />
        <div class="skeleton-line w-16 h-7" />
        <div class="skeleton-line w-20 h-3 mt" />
      </div>
    </template>
    <template v-else-if="variant === 'table'">
      <div class="skeleton-card">
        <div class="skeleton-line w-full h-3 mb" />
        <div v-for="i in (count ?? 5)" :key="i" class="skeleton-row">
          <div class="skeleton-line w-1/4 h-3" />
          <div class="skeleton-line w-1/3 h-3" />
          <div class="skeleton-line w-1/6 h-3" />
        </div>
      </div>
    </template>
    <template v-else-if="variant === 'chart'">
      <div class="skeleton-card skeleton-chart">
        <div class="skeleton-line w-24 h-3 mb" />
        <div class="skeleton-pulse" />
      </div>
    </template>
    <template v-else-if="variant === 'list'">
      <div v-for="i in (count ?? 5)" :key="i" class="skeleton-list-item">
        <div class="skeleton-dot" />
        <div class="skeleton-lines">
          <div class="skeleton-line w-2/3 h-3 mb-sm" />
          <div class="skeleton-line w-1/3 h-2" />
        </div>
      </div>
    </template>
    <template v-else>
      <div class="skeleton-card">
        <div class="skeleton-line w-full h-3 mb" />
        <div class="skeleton-line w-3/4 h-3 mb" />
        <div class="skeleton-line w-1/2 h-3" />
      </div>
    </template>
  </div>
</template>

<style scoped>
.skeleton-wrapper {
  width: 100%;
}

.skeleton-card {
  background: var(--bg-card);
  border-radius: var(--radius-md);
  border: 1px solid var(--border-light);
  padding: 18px;
}

.skeleton-card.mb + .skeleton-card { margin-top: 12px; }

.skeleton-line {
  background: var(--bg-hover);
  border-radius: 3px;
  animation: pulse 1.5s ease-in-out infinite;
}

.skeleton-row {
  display: flex;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid var(--border-light);
}

.skeleton-row:last-child { border-bottom: none; }

.skeleton-chart { height: 200px; display: flex; flex-direction: column; }

.skeleton-pulse {
  flex: 1;
  background: var(--bg-hover);
  border-radius: var(--radius-sm);
  animation: pulse 1.5s ease-in-out infinite;
}

.skeleton-list-item {
  display: flex;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid var(--border-light);
}

.skeleton-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--bg-hover);
  flex-shrink: 0;
  margin-top: 4px;
  animation: pulse 1.5s ease-in-out infinite;
}

.skeleton-lines { flex: 1; }

/* Size utilities */
.w-16 { width: 64px; }
.w-20 { width: 80px; }
.w-24 { width: 96px; }
.w-1\/6 { width: 16.66%; }
.w-1\/4 { width: 25%; }
.w-1\/3 { width: 33.33%; }
.w-2\/3 { width: 66.66%; }
.w-1\/2 { width: 50%; }
.w-3\/4 { width: 75%; }
.w-full { width: 100%; }
.h-2 { height: 8px; }
.h-3 { height: 12px; }
.h-7 { height: 28px; }
.mb { margin-bottom: 10px; }
.mb-sm { margin-bottom: 6px; }
.mt { margin-top: 6px; }

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}
</style>
