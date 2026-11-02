// Thin API client. Token lives in localStorage; every request carries it as a Bearer header.
const TOKEN_KEY = 'ovs_token'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || ''
}

export function setToken(t) {
  localStorage.setItem(TOKEN_KEY, t || '')
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY)
}

async function request(path, { method = 'GET', body, form } = {}) {
  const headers = {}
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`

  let payload
  if (form) {
    payload = form
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }

  const res = await fetch(path, { method, headers, body: payload })
  if (res.status === 204) return null
  const data = await res.json().catch(() => null)
  if (!res.ok) {
    const detail = data && data.detail
    const msg = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map((d) => d.msg).join('; ') : res.statusText
    throw new Error(msg || '请求失败')
  }
  return data
}

export const api = {
  // auth
  guest: () => request('/api/auth/guest', { method: 'POST' }),
  bind: (payload) => request('/api/auth/bind', { method: 'POST', body: payload }),
  login: (payload) => request('/api/auth/login', { method: 'POST', body: payload }),
  me: () => request('/api/auth/me'),

  // templates
  templates: () => request('/api/templates'),
  saveProjectAsTemplate: (pid, body) => request(`/api/templates/from-project/${pid}`, { method: 'POST', body }),
  deleteTemplate: (id) => request(`/api/templates/${id}`, { method: 'DELETE' }),

  // assets (素材库:人物/道具/场景)
  upload: (file, category = 'prop') => {
    const form = new FormData()
    form.append('file', file)
    form.append('category', category)
    return request('/api/assets', { method: 'POST', form })
  },
  asset: (id) => request(`/api/assets/${id}`),
  myAssets: (category) => request('/api/assets' + (category ? `?category=${category}` : '')),
  updateAsset: (id, patch) => request(`/api/assets/${id}`, { method: 'PATCH', body: patch }),
  deleteAsset: (id) => request(`/api/assets/${id}`, { method: 'DELETE' }),

  // projects
  // projects(列表接口现在是分页的:返回 { items, total, page, page_size })
  projects: (page = 1, pageSize = 60) => request(`/api/projects?page=${page}&page_size=${pageSize}`),
  project: (id) => request(`/api/projects/${id}`),
  createProject: (body) => request('/api/projects', { method: 'POST', body }),
  analyzeScript: (body) => request('/api/projects/analyze-script', { method: 'POST', body }),
  createFromScript: (body) => request('/api/projects/from-script', { method: 'POST', body }),
  renameProject: (id, title) => request(`/api/projects/${id}`, { method: 'PATCH', body: { title } }),
  // 项目级成片设置:分辨率 / 帧率(与标题共用同一个 PATCH)
  updateProject: (id, patch) => request(`/api/projects/${id}`, { method: 'PATCH', body: patch }),
  deleteProject: (id) => request(`/api/projects/${id}`, { method: 'DELETE' }),
  updateShot: (pid, sid, patch) => request(`/api/projects/${pid}/shots/${sid}`, { method: 'PATCH', body: patch }),
  deleteShot: (pid, sid) => request(`/api/projects/${pid}/shots/${sid}`, { method: 'DELETE' }),
  renderShot: (pid, sid) => request(`/api/projects/${pid}/shots/${sid}/render`, { method: 'POST' }),
  // 删掉单个分镜已生成的视频 / 删掉合成好的整片
  deleteShotClip: (pid, sid) => request(`/api/projects/${pid}/shots/${sid}/clip`, { method: 'DELETE' }),
  deleteFilm: (pid) => request(`/api/projects/${pid}/film`, { method: 'DELETE' }),
  optimizeShot: (pid, sid, body) => request(`/api/projects/${pid}/shots/${sid}/optimize`, { method: 'POST', body }),
  reorderShots: (pid, shotIds) => request(`/api/projects/${pid}/shots/reorder`, { method: 'POST', body: { shot_ids: shotIds } }),

  // render
  render: (pid) => request(`/api/projects/${pid}/render`, { method: 'POST' }),
  job: (id) => request(`/api/jobs/${id}`),
  cancelJob: (id) => request(`/api/jobs/${id}/cancel`, { method: 'POST' }),
  comfyHealth: () => request('/api/system/comfy'),
  llmHealth: () => request('/api/system/llm')
}
