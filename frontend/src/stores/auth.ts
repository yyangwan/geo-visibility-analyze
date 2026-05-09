import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { getMe, type AuthUser } from '../api/client'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<AuthUser | null>(null)
  const token = ref<string | null>(localStorage.getItem('token'))

  const isLoggedIn = computed(() => !!token.value && !!user.value)

  function setToken(t: string) {
    token.value = t
    localStorage.setItem('token', t)
  }

  function logout() {
    token.value = null
    user.value = null
    localStorage.removeItem('token')
  }

  async function fetchUser() {
    if (!token.value) return
    try {
      const { data } = await getMe()
      user.value = data
    } catch {
      logout()
    }
  }

  return { user, token, isLoggedIn, setToken, logout, fetchUser }
})
