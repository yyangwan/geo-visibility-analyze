import axios from 'axios'

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

// Auth token interceptor
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// Auto-logout on 401
api.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      localStorage.removeItem('token')
      window.location.href = '/login'
    }
    return Promise.reject(err)
  }
)

// Types
export interface Project {
  id: number
  name: string
  industry: string
  created_at: string
}

export interface Brand {
  id: number
  name: string
  aliases: string[]
  is_competitor: boolean
}

export interface Prompt {
  id: number
  text: string
  category: string
  is_auto_generated: boolean
}

export interface Audit {
  id: number
  project_id: number
  status: 'pending' | 'running' | 'completed' | 'failed' | 'partial'
  platforms_json: string[]
  created_at: string
  completed_at: string | null
  error_message: string | null
}

export interface QueryResult {
  id: number
  platform: string
  prompt_text: string | null
  brand_name: string | null
  mention_found: boolean
  mention_position: number | null
  mention_context: string | null
  mention_confidence: number | null
  is_recommended: boolean
  recommendation_rank: number | null
  error: string | null
}

export interface Report {
  id: number
  project_id: number
  audit_id: number
  overall_score: number
  mention_rate: number
  competitor_rank: number | null
  sentiment_positive_rate: number | null
  platform_scores: Record<string, number>
  insights: string[]
  created_at: string
}

// Projects
export const getProjects = () => api.get<Project[]>('/projects')
export const createProject = (data: { name: string; industry?: string }) =>
  api.post<Project>('/projects', data)

// Brands
export const getBrands = (projectId: number) =>
  api.get<Brand[]>(`/projects/${projectId}/brands`)
export const addBrand = (projectId: number, data: { name: string; aliases?: string[]; is_competitor?: boolean }) =>
  api.post<Brand>(`/projects/${projectId}/brands`, data)

// Prompts
export const getPrompts = (projectId: number) =>
  api.get<Prompt[]>(`/projects/${projectId}/prompts`)
export const addPrompt = (projectId: number, data: { text: string; category?: string }) =>
  api.post<Prompt>(`/projects/${projectId}/prompts`, data)
export const deletePrompt = (projectId: number, promptId: number) =>
  api.delete(`/projects/${projectId}/prompts/${promptId}`)

// Audits
export const createAudit = (data: { project_id: number; platforms?: string[] }) =>
  api.post<Audit>('/audits', data)
export const getAudit = (auditId: number) =>
  api.get<Audit>(`/audits/${auditId}`)
export const getAuditResults = (auditId: number) =>
  api.get<QueryResult[]>(`/audits/${auditId}/results`)

// Reports
export const generateReport = (auditId: number) =>
  api.post<Report>(`/audits/${auditId}/report`)
export const getReport = (auditId: number) =>
  api.get<Report>(`/audits/${auditId}/report`)

// Trends
export interface TrendPoint {
  date: string
  overall_score: number
  mention_rate: number
  competitor_rank: number | null
  platform_scores: Record<string, number>
  audit_id: number
}

export const getTrendData = (projectId: number, period = 'daily', limit = 30) =>
  api.get<{ project_id: number; data: TrendPoint[] }>(`/trends/${projectId}`, {
    params: { period, limit },
  })

export const getLatestReport = (projectId: number) =>
  api.get<Report>(`/trends/${projectId}/latest-report`)

export const getAuditsHistory = (projectId: number, limit = 20) =>
  api.get(`/trends/${projectId}/audits-history`, { params: { limit } })

// Report PDF Export
export const exportReportPdf = (reportId: number) =>
  api.get(`/reports/${reportId}/pdf`, { responseType: 'blob' })

// Suggestions
export interface Suggestion {
  id: number
  project_id: number
  report_id: number
  category: string
  title: string
  description: string
  priority: string
  is_resolved: boolean
  created_at: string
}

export const getSuggestions = (projectId: number) =>
  api.get<Suggestion[]>(`/suggestions/${projectId}`)

export const generateSuggestions = (projectId: number) =>
  api.post<Suggestion[]>(`/suggestions/${projectId}/generate`)

export const resolveSuggestion = (suggestionId: number) =>
  api.patch<Suggestion>(`/suggestions/${suggestionId}/resolve`)

export const deleteSuggestion = (suggestionId: number) =>
  api.delete(`/suggestions/${suggestionId}`)

// Prompt Auto-generation
export const generatePrompts = (projectId: number, count = 10) =>
  api.post<Prompt[]>(`/projects/${projectId}/prompts/generate`, {
    project_id: projectId,
    count,
  })

// Auth
export interface AuthUser {
  id: number
  username: string
  is_active: boolean
}

export const register = (username: string, password: string) =>
  api.post<AuthUser>('/auth/register', { username, password })

export const login = (username: string, password: string) => {
  const form = new URLSearchParams()
  form.append('username', username)
  form.append('password', password)
  return api.post<{ access_token: string; token_type: string }>('/auth/login', form, {
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
  })
}

export const getMe = () => api.get<AuthUser>('/auth/me')

// Scheduled Jobs
export interface ScheduledJob {
  id: number
  project_id: number
  cron_expression: string
  platforms_json: string[]
  is_active: boolean
  last_run_at: string | null
  last_audit_id: number | null
  created_at: string
}

export const getSchedules = () =>
  api.get<ScheduledJob[]>('/schedules')
export const createSchedule = (data: { project_id: number; cron_expression: string; platforms?: string[] }) =>
  api.post<ScheduledJob>('/schedules', data)
export const toggleSchedule = (jobId: number) =>
  api.patch<ScheduledJob>(`/schedules/${jobId}/toggle`)
export const deleteSchedule = (jobId: number) =>
  api.delete(`/schedules/${jobId}`)

export default api
