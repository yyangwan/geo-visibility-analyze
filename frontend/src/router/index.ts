import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('../views/auth/LoginView.vue'),
      meta: { public: true },
    },
    {
      path: '/',
      redirect: '/dashboard',
    },
    {
      path: '/dashboard',
      name: 'dashboard',
      component: () => import('../views/dashboard/DashboardView.vue'),
    },
    {
      path: '/trends',
      name: 'trends',
      component: () => import('../views/trends/TrendsView.vue'),
    },
    {
      path: '/competitors',
      name: 'competitors',
      component: () => import('../views/competitors/CompetitorsView.vue'),
    },
    {
      path: '/prompts',
      name: 'prompts',
      component: () => import('../views/prompts/PromptsView.vue'),
    },
    {
      path: '/settings',
      name: 'settings',
      component: () => import('../views/settings/SettingsView.vue'),
    },
    {
      path: '/suggestions',
      name: 'suggestions',
      component: () => import('../views/suggestions/SuggestionsView.vue'),
    },
    {
      path: '/analysis',
      name: 'analysis',
      component: () => import('../views/analysis/AnalysisView.vue'),
    },
  ],
})

router.beforeEach((to) => {
  if (to.meta.public) return true
  const token = localStorage.getItem('token')
  if (!token) {
    return { name: 'login' }
  }
  return true
})

export default router
