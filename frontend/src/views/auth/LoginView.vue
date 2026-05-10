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
    <!-- Product Narrative -->
    <div class="narrative">
      <div class="narrative-content">
        <h1>您的品牌在 AI 搜索中被推荐了吗？</h1>
        <p class="narrative-desc">
          智见实时监测 DeepSeek、通义千问、豆包、Kimi、腾讯元宝
          五大 AI 平台，量化品牌可见性评分、提及率与竞品排名。
        </p>
        <div class="sample-cards">
          <div class="sample-card">
            <div class="sample-score">
              <span class="score-value">78</span>
              <span class="score-suffix">/100</span>
            </div>
            <div class="sample-label">综合可见性</div>
          </div>
          <div class="sample-card">
            <div class="sample-score">
              <span class="score-value">63%</span>
            </div>
            <div class="sample-label">品牌提及率</div>
          </div>
          <div class="sample-card">
            <div class="sample-score">
              <span class="score-value">#2</span>
            </div>
            <div class="sample-label">竞品排名</div>
          </div>
        </div>
        <ul class="feature-list">
          <li>6 大 AI 平台实时审计</li>
          <li>竞品对比与趋势追踪</li>
          <li>AI 驱动的优化建议</li>
        </ul>
      </div>
    </div>

    <!-- Auth Form -->
    <div class="login-card">
      <div class="login-header">
        <div class="logo">
          <span class="dot"></span>
          智<span class="accent">见</span>
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
  align-items: stretch;
  background: var(--bg-base);
}

/* Narrative Panel */
.narrative {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 48px 64px;
  background: linear-gradient(135deg, #0d1117 0%, #111827 50%, #0f172a 100%);
  border-right: 1px solid var(--border);
}

.narrative-content {
  max-width: 480px;
}

.narrative h1 {
  font-size: 28px;
  font-weight: 700;
  line-height: 1.3;
  margin-bottom: 16px;
  background: linear-gradient(135deg, var(--text-primary) 0%, var(--accent) 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}

.narrative-desc {
  font-size: 14px;
  color: var(--text-secondary);
  line-height: 1.7;
  margin-bottom: 32px;
}

.sample-cards {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  margin-bottom: 32px;
}

.sample-card {
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 16px 14px;
  text-align: center;
}

.score-value {
  font-size: 24px;
  font-weight: 700;
  color: var(--accent);
}

.score-suffix {
  font-size: 13px;
  color: var(--text-muted);
}

.sample-label {
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 4px;
}

.feature-list {
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.feature-list li {
  font-size: 13px;
  color: var(--text-secondary);
  padding-left: 20px;
  position: relative;
}

.feature-list li::before {
  content: '✓';
  position: absolute;
  left: 0;
  color: var(--accent);
  font-weight: 600;
}

.login-card {
  width: 100%;
  max-width: 400px;
  background: var(--bg-card);
  border-left: 1px solid var(--border);
  border-radius: 0;
  padding: 48px 36px;
  display: flex;
  flex-direction: column;
  justify-content: center;
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

@media (max-width: 768px) {
  .login-page {
    flex-direction: column;
  }
  .narrative {
    padding: 32px 24px;
    border-right: none;
    border-bottom: 1px solid var(--border);
  }
  .narrative h1 {
    font-size: 22px;
  }
  .sample-cards {
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
  }
  .login-card {
    max-width: none;
    border-left: none;
    padding: 32px 24px;
  }
}
</style>
