import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  getProjects,
  getBrands,
  getPrompts,
  getReport,
  type Project,
  type Brand,
  type Prompt,
  type Report,
} from '../api/client'

export const useProjectStore = defineStore('project', () => {
  const projects = ref<Project[]>([])
  const currentProject = ref<Project | null>(null)
  const brands = ref<Brand[]>([])
  const prompts = ref<Prompt[]>([])
  const report = ref<Report | null>(null)
  const loading = ref(false)

  async function fetchProjects() {
    loading.value = true
    try {
      const { data } = await getProjects()
      projects.value = data
      if (!currentProject.value && data.length > 0) {
        await selectProject(data[0])
      }
    } finally {
      loading.value = false
    }
  }

  async function selectProject(project: Project) {
    currentProject.value = project
    await Promise.all([fetchBrands(project.id), fetchPrompts(project.id)])
  }

  async function fetchBrands(projectId: number) {
    const { data } = await getBrands(projectId)
    brands.value = data
  }

  async function fetchPrompts(projectId: number) {
    const { data } = await getPrompts(projectId)
    prompts.value = data
  }

  async function fetchReport(auditId: number) {
    try {
      const { data } = await getReport(auditId)
      report.value = data
    } catch {
      report.value = null
    }
  }

  return {
    projects,
    currentProject,
    brands,
    prompts,
    report,
    loading,
    fetchProjects,
    selectProject,
    fetchBrands,
    fetchPrompts,
    fetchReport,
  }
})
