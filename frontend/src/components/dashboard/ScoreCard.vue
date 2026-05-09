<script setup lang="ts">
defineProps<{
  label: string
  value: string | number
  suffix?: string
  status: 'good' | 'warn' | 'alert'
  change?: string
  changeDir?: 'up' | 'down' | 'flat'
  benchmark?: string
  alertBadge?: string
}>()
</script>

<template>
  <div class="score-card" :class="status">
    <div class="label">{{ label }}</div>
    <div class="value">
      {{ value }}<span v-if="suffix" class="suffix">{{ suffix }}</span>
      <span v-if="alertBadge" class="alert-badge">{{ alertBadge }}</span>
    </div>
    <div class="meta">
      <span v-if="change" class="change" :class="changeDir || 'flat'">
        <span class="arrow">{{ changeDir === 'up' ? '↑' : changeDir === 'down' ? '↓' : '→' }}</span>
        {{ change }}
      </span>
      <span v-if="benchmark" class="benchmark">{{ benchmark }}</span>
    </div>
  </div>
</template>

<style scoped>
.score-card {
  background: var(--bg-card);
  border-radius: var(--radius-md);
  padding: 16px 18px;
  border: 1px solid var(--border-light);
  cursor: pointer;
  transition: all 0.15s ease;
}

.score-card:hover {
  border-color: var(--accent);
  transform: translateY(-1px);
}

.label {
  font-size: 10px;
  color: var(--text-muted);
  margin-bottom: 8px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  font-weight: 600;
}

.value {
  font-size: 26px;
  font-weight: 700;
  font-family: "Inter", monospace;
}

.suffix {
  font-size: 13px;
  color: var(--text-muted);
}

.alert-badge {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  margin-left: 8px;
  background: rgba(239, 68, 68, 0.15);
  color: var(--status-bad);
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 10px;
  font-weight: 600;
  vertical-align: middle;
}

.meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}

.change {
  font-size: 11px;
  font-weight: 500;
  display: flex;
  align-items: center;
  gap: 3px;
}

.arrow {
  font-size: 10px;
}

.change.up { color: var(--status-good); }
.change.down { color: var(--status-bad); }
.change.flat { color: var(--text-muted); }

.benchmark {
  font-size: 10px;
  color: var(--text-muted);
}

.good {
  border-left: 3px solid var(--status-good);
}
.good .value {
  color: var(--status-good);
}

.warn {
  border-left: 3px solid var(--status-warn);
}
.warn .value {
  color: var(--status-warn);
}

.alert {
  border-left: 3px solid var(--status-bad);
}
.alert .value {
  color: var(--status-bad);
}
</style>
