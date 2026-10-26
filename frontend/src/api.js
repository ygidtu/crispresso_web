const BASE = '/api'

async function req(url, opts) {
  const res = await fetch(BASE + url, opts)
  const ct = res.headers.get('content-type') || ''
  const data = ct.includes('application/json') ? await res.json() : await res.text()
  if (!res.ok) {
    const msg = (data && (data.error || data.message)) || res.statusText
    throw new Error(msg)
  }
  return data
}

export const api = {
  health: () => req('/health'),
  batches: () => req('/batches'),

  // 上传 Excel 并探测 sheet / 列 / 预览行
  preview: async (file) => {
    const fd = new FormData()
    fd.append('xlsx', file)
    return req('/preview', { method: 'POST', body: fd })
  },

  // 创建任务（带上传文件）
  create: async (payload, file) => {
    const fd = new FormData()
    for (const [k, v] of Object.entries(payload)) {
      if (v === undefined || v === null) continue
      fd.append(k, v)
    }
    if (file) fd.append('xlsx', file)
    return req('/jobs', { method: 'POST', body: fd })
  },

  // raw_root 下拉：runs_root 子文件夹 + fastq 父级探测
  runs: () => req('/runs'),
  fastqRoot: (dir) => req('/runs/fastq_root?dir=' + encodeURIComponent(dir)),

  start: (id, steps) => req(`/jobs/${id}/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(steps ? { steps } : {}),
  }),
  get: (id) => req(`/jobs/${id}`),
  list: () => req('/jobs').then((r) => r.jobs || []),
  log: (id, tail) => req(`/jobs/${id}/log?tail=${tail || 2000}`),
  remove: (id) => req(`/jobs/${id}`, { method: 'DELETE' }),

  // 结果下载（返回完整 URL，前端用 <a> 或 window.open）
  downloadUrl: (id, path) => `${BASE}/jobs/${id}/download?path=${encodeURIComponent(path)}`,
  packUrl: (id) => `${BASE}/jobs/${id}/pack`,

  // ===== OSS 数据下载（信封加密：RSA-OAEP 包 AES-256-GCM）=====
  ossPubkey: () => req('/oss/pubkey'),
  ossDownload: (payload) => req('/oss/download', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  ossTask: (id) => req(`/oss/tasks/${id}`),
  ossLog: (id, tail) => req(`/oss/tasks/${id}/log?tail=${tail || 100}`),
  ossTaskList: () => req('/oss/tasks').then((r) => r.tasks || []),
  ossLogUrl: (id) => `${BASE}/oss/tasks/${id}/log`,
  ossRemove: (id) => req(`/oss/tasks/${id}`, { method: 'DELETE' }),
  ossMerge: (keep, drop) => req('/oss/merge', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ keep, drop }),
  }),
}
