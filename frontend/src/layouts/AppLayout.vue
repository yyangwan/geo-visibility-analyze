<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const collapsed = ref(false)

interface NavItem {
  path: string
  icon: string
  label: string
  disabled?: boolean
}

const navItems: { section: string; items: (NavItem & { badge?: number })[] }[] = [
  { section: '分析', items: [
    { path: '/dashboard', icon: '📊', label: '可见性概览' },
    { path: '/trends', icon: '📈', label: '趋势追踪', badge: 3 },
    { path: '/competitors', icon: '⚔️', label: '竞品对比' },
    { path: '/prompts', icon: '🔍', label: 'Prompt管理' },
  ]},
  { section: '优化', items: [
    { path: '/suggestions', icon: '💡', label: '优化建议' },
    { path: '/settings', icon: '📄', label: '报告导出' },
  ]},
  { section: '设置', items: [
    { path: '/settings', icon: '⚙️', label: '平台配置' },
  ]},
]

function handleLogout() {
  auth.logout()
  router.push('/login')
}

onMounted(() => {
  if (auth.token && !auth.user) {
    auth.fetchUser()
  }
})
</script>

<template>
  <nav class="sidebar" :class="{ collapsed }">
    <div class="sidebar-logo" @click="collapsed = !collapsed">
      <div class="dot"></div>
      <template v-if="!collapsed">AI<span>Scope</span></template>
    </div>

    <template v-for="group in navItems" :key="group.section">
      <div v-if="!collapsed" class="nav-section">{{ group.section }}</div>
      <RouterLink
        v-for="item in group.items"
        :key="item.label"
        :to="item.disabled ? '' : item.path"
        class="nav-item"
        :class="{
          active: route.path === item.path,
          disabled: item.disabled,
        }"
      >
        <span class="nav-icon">{{ item.icon }}</span>
        <span v-if="!collapsed">{{ item.label }}</span>
        <span v-if="!collapsed && item.badge" class="nav-badge">{{ item.badge }}</span>
      </RouterLink>
    </template>

    <div class="sidebar-spacer"></div>

    <div v-if="auth.user && !collapsed" class="sidebar-user">
      <div class="user-info">
        <span class="user-name">{{ auth.user.username }}</span>
      </div>
      <button class="btn-logout" @click="handleLogout">退出</button>
    </div>
  </nav>

  <main class="main-content">
    <slot />
  </main>
</template>

<style scoped>
.sidebar {
  width: 220px;
  background: var(--bg-sidebar);
  padding: 20px 0;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  border-right: 1px solid var(--border);
  transition: width 0.2s ease;
}

.sidebar.collapsed {
  width: 56px;
}

.sidebar-logo {
  padding: 0 20px 24px;
  font-size: 16px;
  font-weight: 700;
  color: var(--text-primary);
  letter-spacing: 0.5px;
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}

.sidebar-logo .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
  flex-shrink: 0;
}

.sidebar-logo span {
  color: var(--accent);
}

.nav-section {
  padding: 16px 20px 6px;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 1.5px;
  color: var(--text-muted);
  font-weight: 600;
}

.nav-item {
  padding: 9px 20px;
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  border-left: 2px solid transparent;
  color: var(--text-secondary);
  text-decoration: none;
  transition: all 0.15s ease;
}

.nav-item:hover:not(.disabled) {
  color: var(--text-primary);
  background: var(--bg-hover);
}

.nav-item.active {
  background: var(--accent-dim);
  border-left-color: var(--accent);
  color: var(--accent);
}

.nav-item.disabled {
  color: var(--text-muted);
  opacity: 0.5;
  cursor: default;
}

.nav-icon {
  flex-shrink: 0;
  width: 16px;
  text-align: center;
  font-size: 13px;
}

.nav-badge {
  margin-left: auto;
  background: var(--accent-dim);
  color: var(--accent);
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 10px;
  font-weight: 600;
}

.sidebar-spacer {
  flex: 1;
}

.sidebar-user {
  padding: 12px 16px;
  margin: 0 8px;
  border-top: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.user-info {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  min-width: 0;
}

.user-name {
  font-size: 11px;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.btn-logout {
  padding: 3px 8px;
  border-radius: 4px;
  font-size: 10px;
  background: transparent;
  color: var(--text-muted);
  border: 1px solid var(--border);
  cursor: pointer;
  transition: all 0.15s;
  flex-shrink: 0;
}

.btn-logout:hover {
  color: #ef4444;
  border-color: #ef4444;
}

.main-content {
  flex: 1;
  overflow-y: auto;
  padding: 24px 28px;
}
</style>
