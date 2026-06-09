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

// Auto-logout on 401 + network error handling
api.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      localStorage.removeItem('token')
      window.location.href = '/login'
    }
    // Network error (server unreachable, DNS failure, CORS block)
    if (!err.response) {
      err.response = { data: { detail: '网络连接失败，请检查网络后重试' } }
    }
    return Promise.reject(err)
  }
)

// Types
export interface Project {
  id: number
  name: string
  industry: string
  product_category: string
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
export const createProject = (data: { name: string; industry?: string; product_category?: string }) =>
  api.post<Project>('/projects', data)
export const updateProject = (id: number, data: Partial<{ name: string; industry: string; product_category: string }>) =>
  api.patch<Project>(`/projects/${id}`, data)

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
export interface SuggestionDetail {
  action_channel?: string
  action_type?: string
  outline?: string[]
  keywords?: string[]
  timeline?: { week: string; task: string }[]
  competitor_ref?: string
  expected_outcome?: string
}

export interface Suggestion {
  id: number
  project_id: number
  report_id: number
  category: string
  title: string
  description: string
  priority: string
  is_resolved: boolean
  detail: SuggestionDetail | null
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
export const generatePrompts = (projectId: number, count = 10, productCategory = '') =>
  api.post<Prompt[]>(`/projects/${projectId}/prompts/generate`, {
    project_id: projectId,
    count,
    product_category: productCategory,
  })

// Audit SSE
export interface AuditProgressEvent {
  type: 'platform_start' | 'platform_done' | 'platform_error' | 'audit_done' | 'audit_failed'
  platform?: string
  error?: string
}

export function createAuditEventSource(auditId: number): EventSource {
  const baseURL = import.meta.env.VITE_API_BASE_URL || '/api'
  const token = localStorage.getItem('token')
  const url = `${baseURL}/audits/${auditId}/events${token ? `?token=${token}` : ''}`
  return new EventSource(url)
}

// Platforms
export interface PlatformInfo {
  key: string
  label: string
  configured: boolean
}

export const getPlatforms = () =>
  api.get<PlatformInfo[]>('/platforms')

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

// Analysis
export interface ResponseAnalysis {
  id: number
  response_record_id: number
  platform: string | null
  prompt_text: string | null
  cited_sources: Array<{ domain: string; authority_score?: number }>
  brand_sentiment: string | null
  brand_attributes: string[]
  topics_covered: string[]
  answer_structure: string | null
  competitor_refs: string[]
  analysis_model: string
  status: string
  created_at: string
}

export interface ContentIntelligence {
  topic_distribution: Record<string, number>
  sentiment_breakdown: Record<string, number>
  answer_structure_distribution: Record<string, number>
  top_cited_sources: Array<{ domain: string; total_count: number; authority_avg: number }>
  brand_positioning_heatmap: Record<string, Record<string, string>>
  token_cost_summary: { total_prompt_tokens: number; total_completion_tokens: number }
  analysis_status: Record<string, number>
  total_responses: number
  analyzed_responses: number
}

export const getAuditAnalysis = (auditId: number) =>
  api.get<ResponseAnalysis[]>(`/analysis/audits/${auditId}/analysis`)

export const triggerAnalysis = (auditId: number) =>
  api.post(`/analysis/audits/${auditId}/analyze`)

export const retryAnalysis = (auditId: number) =>
  api.post(`/analysis/audits/${auditId}/analyze/retry`)

export const getContentIntelligence = (projectId: number) =>
  api.get<ContentIntelligence>(`/analysis/projects/${projectId}/content-intelligence`)

// Strategic Intelligence
export interface SourceAuthorityTrends {
  audits: Array<{ audit_id: number; date: string; total_sources: number }>
  domain_trends: Array<{ domain: string; data: Array<{ audit_id: number; count: number; authority_avg: number }> }>
  platform_preferences: Array<{ platform: string; top_domains: Array<{ domain: string; count: number }> }>
  authority_trend: Record<string, string[]>
}

export interface CompetitorPositioning {
  brands: Array<{
    name: string
    is_competitor: boolean
    mention_frequency: number
    sentiment_positive_rate: number
    avg_authority: number
    mention_count: number
    trajectory: Array<{ audit_id: number; date: string; mention_rate: number; sentiment_positive_rate: number }>
  }>
  quadrant_labels: Record<string, string>
}

export interface AnswerStructureEvolution {
  audits: Array<{ audit_id: number; date: string }>
  structure_distribution: Record<string, Array<{ audit_id: number; count: number; pct: number }>>
  platform_structure: Record<string, Record<string, number>>
  correlation: Record<string, { mention_rate: number; avg_position: number | null }>
  transitions: Array<{ audit_id: number; platform: string; prev_structure: string | null; new_structure: string }>
}

export interface MultiAuditComparison {
  audits: Array<{
    audit_id: number
    date: string
    overall_score: number
    mention_rate: number
    sentiment_breakdown: Record<string, number>
    top_sources: Array<{ domain: string; count: number }>
    competitor_mention_rates: Array<{ brand: string; mention_rate: number }>
    structure_distribution: Record<string, number>
    topic_distribution: Record<string, number>
  }>
  diffs: {
    mention_rate_delta: number
    score_delta: number
    source_changes: { added: string[]; removed: string[] }
    competitor_changes: Array<{ brand: string; delta: number }>
  }
}

export const getSourceAuthorityTrends = (projectId: number, limit = 10) =>
  api.get<SourceAuthorityTrends>(`/strategic/projects/${projectId}/source-authority-trends`, { params: { limit } })

export const getCompetitorPositioning = (projectId: number) =>
  api.get<CompetitorPositioning>(`/strategic/projects/${projectId}/competitor-positioning`)

export const getStructureEvolution = (projectId: number, limit = 10) =>
  api.get<AnswerStructureEvolution>(`/strategic/projects/${projectId}/structure-evolution`, { params: { limit } })

export const compareAudits = (projectId: number, auditIds: number[]) =>
  api.post<MultiAuditComparison>(`/strategic/projects/${projectId}/compare-audits`, { audit_ids: auditIds })

export default api
