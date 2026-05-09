<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { login, register } from '../../api/client'

const router = useRouter()
const auth = useAuthStore()

const isRegister = ref(false)
const username = ref('')
const password = ref('')
const error = ref('')
const loading = ref(false)

async function handleSubmit() {
  if (!username.value.trim() || !password.value) {
    error.value = '请输入用户名和密码'
    return
  }
  error.value = ''
  loading.value = true
  try {
    if (isRegister.value) {
      await register(username.value.trim(), password.value)
    }
    const { data } = await login(username.value.trim(), password.value)
    auth.setToken(data.access_token)
    await auth.fetchUser()
    router.push('/')
  } catch (e: any) {
    const detail = e?.response?.data?.detail
    if (typeof detail === 'string') {
      error.value = detail
    } else {
      error.value = isRegister.value ? '注册失败，用户名可能已存在' : '用户名或密码错误'
    }
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-header">
        <div class="logo">
          <span class="dot"></span>
          AI<span class="accent">Scope</span>
        </div>
        <p class="subtitle">AI搜索可见性分析平台</p>
      </div>

      <form @submit.prevent="handleSubmit" class="login-form">
        <div class="field">
          <label>用户名</label>
          <input
            v-model="username"
            type="text"
            placeholder="请输入用户名"
            autocomplete="username"
          />
        </div>
        <div class="field">
          <label>密码</label>
          <input
            v-model="password"
            type="password"
            placeholder="请输入密码"
            autocomplete="current-password"
          />
        </div>
        <div v-if="error" class="error-msg">{{ error }}</div>
        <button type="submit" class="btn-login" :disabled="loading">
          {{ loading ? '处理中...' : (isRegister ? '注册' : '登录') }}
        </button>
      </form>

      <div class="switch-mode">
        <span v-if="!isRegister">
          还没有账号？<button class="link" @click="isRegister = true; error = ''">注册</button>
        </span>
        <span v-else>
          已有账号？<button class="link" @click="isRegister = false; error = ''">登录</button>
        </span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-base);
}

.login-card {
  width: 380px;
  background: var(--bg-card);
  border: 1px solid var(--border-light);
  border-radius: 12px;
  padding: 36px 32px;
}

.login-header {
  text-align: center;
  margin-bottom: 28px;
}

.logo {
  font-size: 22px;
  font-weight: 700;
  color: var(--text-primary);
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}

.dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--accent);
}

.accent { color: var(--accent); }

.subtitle {
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 6px;
}

.field {
  margin-bottom: 16px;
}

.field label {
  display: block;
  font-size: 11px;
  color: var(--text-secondary);
  margin-bottom: 6px;
  font-weight: 500;
}

.field input {
  width: 100%;
  padding: 10px 12px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--bg-input);
  color: var(--text-primary);
  font-size: 13px;
  outline: none;
  transition: border-color 0.15s;
  box-sizing: border-box;
}

.field input:focus {
  border-color: var(--accent);
}

.error-msg {
  font-size: 12px;
  color: var(--status-bad);
  margin-bottom: 12px;
  padding: 6px 10px;
  background: rgba(239,68,68,0.08);
  border-radius: 4px;
}

.btn-login {
  width: 100%;
  padding: 10px;
  border-radius: 6px;
  border: none;
  background: var(--accent);
  color: var(--bg-base);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s;
}

.btn-login:hover:not(:disabled) { background: #00e8bb; }
.btn-login:disabled { opacity: 0.5; cursor: not-allowed; }

.switch-mode {
  text-align: center;
  margin-top: 18px;
  font-size: 12px;
  color: var(--text-muted);
}

.link {
  background: none;
  border: none;
  color: var(--accent);
  cursor: pointer;
  font-size: 12px;
  font-weight: 500;
}
</style>
