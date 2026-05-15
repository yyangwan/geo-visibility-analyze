<template>
  <div class="sso-callback">
    <div class="loading">
      <div class="spinner"></div>
      <p>Logging in via GeniLink...</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '../../stores/auth'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()

onMounted(async () => {
  const code = route.query.code as string

  if (!code) {
    router.push({ name: 'login' })
    return
  }

  try {
    // Exchange code for token via our backend
    const backendUrl = import.meta.env.VITE_API_URL || '/api'
    const res = await fetch(`${backendUrl}/auth/sso/callback?code=${encodeURIComponent(code)}`)

    if (!res.ok) {
      throw new Error('SSO callback failed')
    }

    const { access_token } = await res.json()

    // Store token and redirect
    localStorage.setItem('token', access_token)
    await authStore.fetchUser()
    router.push('/dashboard')
  } catch (err) {
    console.error('SSO callback error:', err)
    router.push({ name: 'login' })
  }
})
</script>

<style scoped>
.sso-callback {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
}

.loading {
  text-align: center;
  color: #94a3b8;
}

.spinner {
  width: 32px;
  height: 32px;
  border: 3px solid #334155;
  border-top-color: #60a5fa;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  margin: 0 auto 16px;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>
