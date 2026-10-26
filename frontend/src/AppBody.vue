<script setup>
import { ref, computed, h, onMounted, onUnmounted, watch, nextTick } from 'vue'
import {
  NCard, NFlex, NGrid, NGridItem, NFormItem, NInput, NInputNumber, NButton, NSelect,
  NUpload, NCheckbox, NSwitch, NTag, NSteps, NStep, NText, NSpin, NEmpty, NIcon, NSpace,
  NTabs, NTabPane, NDivider, NPopconfirm, NProgress,
  useMessage,
} from 'naive-ui'
import { api } from './api.js'
import forge from 'node-forge'

const message = useMessage()

const activeTab = ref('analysis')

// CRISPResso 可选参数留空 = 使用 CRISPResso2 原生默认(qw_center=-3 / qw_size=1 / nw_gap_open=-20)
// raw_root 不设前端默认值：由"选择 run → 后端 /api/runs/fastq_root 探测"产生，避免向前端暴露服务器路径
const form = ref({
  sheet: [], batch: 'yw4', raw_root: '', scores: [25, 30, 35, 40, 45, 50],
  workers: 32, force: false,
  ignore_substitutions: false,
  quantification_window_center: null, quantification_window_size: null,
  needleman_wunsch_gap_open: null,
  notify_email: '',
})
// 预设参数组合：Cas9 / Cas12f1 / IsCB（仅覆盖定量窗口，gap_open 保持 CRISPResso2 默认）
const CRISPR_PRESETS = {
  Cas9: { quantification_window_center: -3, quantification_window_size: 10 },
  Cas12f1: { quantification_window_center: 1, quantification_window_size: 10 },
  IsCB: { quantification_window_center: -9, quantification_window_size: 6 },
}
function applyPreset(name) {
  const p = CRISPR_PRESETS[name]
  if (!p) return
  form.value.quantification_window_center = p.quantification_window_center
  form.value.quantification_window_size = p.quantification_window_size
  message.success(`已填入 ${name} 参数：qw_center=${p.quantification_window_center > 0 ? '+' + p.quantification_window_center : p.quantification_window_center}，qw_size=${p.quantification_window_size}（gap_open 保持 CRISPResso2 默认）`)
}
function resetCrispressoDefaults() {
  form.value.quantification_window_center = null
  form.value.quantification_window_size = null
  form.value.needleman_wunsch_gap_open = null
  message.success('已重置为 CRISPResso2 默认值（qw_center=-3 / qw_size=1 / nw_gap_open=-20）')
}
const sheets = ref([])
const sheetErrors = ref({})
const sheetOptions = computed(() => sheets.value.map((s) => ({ label: s.name, value: s.name })))
const SCORE_VALUES = [20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80]
const scoreOptions = computed(() => SCORE_VALUES.map((s) => ({ label: String(s), value: s })))
// ---- raw_root 下拉：runs_root 子文件夹 + fastq 父级自动探测 ----
const runs = ref([])
const selectedRun = ref(null)
const detecting = ref(false)
const lastDetect = ref(null)

async function loadRuns() {
  try {
    const res = await api.runs()
    runs.value = res.runs || []
  } catch { /* ignore */ }
}

// run 文件夹名较长：下拉菜单自适应宽度 + 选项文字换行，保证完整可读；
// 并关联 OSS 下载登记信息（等级负责人 / 数据情况说明）作为选项第二行
const runsOptions = computed(() => runs.value.map((r) => {
  const meta = runMetaMap.value[r.name] || {}
  return { label: r.name, value: r.path, owner: meta.owner || '', desc: meta.desc || '' }
}))

const selectedRunMeta = computed(() => {
  if (!selectedRun.value) return null
  const name = selectedRun.value.replace(/\/+$/, '').split('/').pop()
  return runMetaMap.value[name] || null
})

// 仅显示 run 文件夹名，避免向前端暴露服务器目录结构
const selectedRunName = computed(() =>
  (selectedRun.value || '').replace(/\/+$/, '').split('/').pop())

const renderRunLabel = (option) => {
  const metas = []
  if (option.owner) metas.push(`负责人: ${option.owner}`)
  if (option.desc) metas.push(option.desc)
  return h('div', { style: 'padding:4px 0;' }, [
    h('div', { style: 'white-space:normal; word-break:break-all; line-height:1.5;' }, String(option.label ?? '')),
    metas.length
      ? h(NText, { depth: 3, tag: 'div', style: 'font-size:12px; white-space:normal; word-break:break-all; line-height:1.5;' },
          { default: () => metas.join(' · ') })
      : null,
  ])
}

async function onSelectRun(path) {
  selectedRun.value = path || null
  lastDetect.value = null
  if (!path) { form.value.raw_root = ''; return }
  detecting.value = true
  try {
    const res = await api.fastqRoot(path)
    if (res.fastq_root) {
      form.value.raw_root = res.fastq_root
      lastDetect.value = { samples: res.samples }
      message.success(`已定位 fastq 目录（${res.samples} 个样本子目录）`)
    } else {
      form.value.raw_root = path
      message.warning('该文件夹内未探测到 fastq（*.fq.gz），请确认数据已下载/解压')
    }
  } catch (e) {
    message.error('探测失败: ' + e.message)
  } finally {
    detecting.value = false
  }
}
const uploadFile = ref(null)
const uploading = ref(false)

const jobs = ref([])
const selectedId = ref(null)
const job = ref(null)
const logText = ref('')
const polling = ref(false)
let pollTimer = null

const STEP_LABEL = { config: '拆分配置', amplicons: '提取 amplicon', split: '拆分 fastq', crispresso: 'CRISPResso', collect: '汇总+绘图', pack: '打包' }

const selectedJob = computed(() => jobs.value.find((j) => j.id === selectedId.value) || null)

const stepStatus = (name) => (job.value && job.value.steps[name]) || 'pending'

const stepDot = (s) => {
  if (s === 'running') return 'process'
  if (s === 'done') return 'finish'
  if (s === 'failed') return 'error'
  return 'wait'
}

// 是否存在失败步骤（用于展示错误日志下载）
const hasFailedStep = computed(() =>
  !!job.value && Object.values(job.value.steps).some((s) => s === 'failed'))

// 查询的任务是否存在失败步骤
const queryHasFailed = computed(() =>
  !!queryJob.value && Object.values(queryJob.value.steps).some((s) => s === 'failed'))

async function refreshJobs() {
  try { jobs.value = await api.list() } catch { /* ignore */ }
}

async function onFileChange(data) {
  const file = data && data.file ? (data.file.raw || data.file.file) : null
  if (!file) return
  uploadFile.value = file
  uploading.value = true
  sheetErrors.value = {}
  try {
    const res = await api.preview(file)
    sheets.value = res.sheets || []
    const invalid = {}
    for (const s of sheets.value) {
      if (!s.ok) invalid[s.name] = s.errors || []
    }
    sheetErrors.value = invalid
    const validNames = sheets.value.filter((s) => s.ok).map((s) => s.name)
    if (validNames.length) {
      form.value.sheet = validNames
    } else if (sheets.value.length) {
      form.value.sheet = sheets.value.map((s) => s.name)
    }
    const nInvalid = Object.keys(invalid).length
    if (nInvalid === sheets.value.length && sheets.value.length) {
      message.error('Excel 格式不符合要求，请检查列结构后重新上传')
    } else if (nInvalid) {
      message.warning(`部分 sheet 缺少必需列：${Object.keys(invalid).join('、')}（已自动选中通过校验的 sheet）`)
    } else {
      message.success('已解析 Excel')
    }
  } catch (e) {
    message.error('解析失败: ' + e.message)
  } finally {
    uploading.value = false
  }
}

async function createAndRun() {
  if (!uploadFile.value) { message.warning('请先上传 metainfo Excel'); return }
  if (!form.value.raw_root.trim()) { message.warning('请填写原始 fastq 根目录'); return }
  if (!form.value.sheet || !form.value.sheet.length) { message.warning('请选择/确认 sheet'); return }
  try {
    const created = await api.create({
      sheet: form.value.sheet.join(','), batch: form.value.batch, raw_root: form.value.raw_root.trim(),
      scores: form.value.scores.join(' '), workers: form.value.workers, force: form.value.force,
      ignore_substitutions: form.value.ignore_substitutions,
      quantification_window_center: form.value.quantification_window_center ?? null,
      quantification_window_size: form.value.quantification_window_size ?? null,
      needleman_wunsch_gap_open: form.value.needleman_wunsch_gap_open ?? null,
      notify_email: (form.value.notify_email || '').trim(),
    }, uploadFile.value)
    await api.start(created.job_id)
    selectedId.value = created.job_id
    message.success('任务已创建并开始运行')
    startPolling(created.job_id)
    await refreshJobs()
  } catch (e) {
    message.error('创建失败: ' + e.message)
  }
}

function startPolling(id) {
  polling.value = true
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = setInterval(() => poll(id), 2000)
}

async function poll(id) {
  try {
    job.value = await api.get(id)
    logText.value = await api.log(id, 3000)
    if (job.value.finished) {
      polling.value = false
      if (pollTimer) clearInterval(pollTimer)
      await refreshJobs()
      // 完成后保留详情展示（不清空 selectedId），避免“几秒后详情自动消失”；
      // 用户点击其它任务或再次点击该任务才切换/收起
    }
  } catch { /* ignore transient errors */ }
}

function stop(id) {
  polling.value = false
  if (pollTimer) clearInterval(pollTimer)
}

function download(kind, id) {
  const paths = { csv: 'split/editing_efficiency.xlsx', xlsx: '编辑效率_with_score.xlsx' }
  if (kind === 'pack') { window.open(api.packUrl(id), '_blank'); return }
  const a = document.createElement('a')
  a.href = api.downloadUrl(id, paths[kind])
  a.download = paths[kind]
  a.click()
}

// 下载运行日志（含失败步骤的 FAIL/ERR 行），用于排查错误原因
function downloadLog(id) {
  const a = document.createElement('a')
  a.href = api.downloadUrl(id, 'run.log')
  a.download = `${id}_run.log`
  a.click()
}

// ---- 删除任务：只物理释放未被其他任务引用的产物，共享缓存自动保留 ----
function fmtBytes(n) {
  if (n == null) return ''
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++ }
  return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${u[i]}`
}

async function deleteJob(id) {
  try {
    const r = await api.remove(id)
    const freed = fmtBytes(r.freed_exclusive_bytes)
    let msg = `已删除 ${r.deleted}（独占产物释放 ${freed}，表观 ${fmtBytes(r.apparent_bytes)}）`
    if (r.cache_pruned && r.cache_pruned.length) msg += `；连带清理无引用缓存 ${r.cache_pruned.length} 项`
    if (r.cache_kept) msg += `；保留仍被引用缓存 ${r.cache_kept} 项`
    if (r.cache_note) msg += `；${r.cache_note}`
    message.success(msg)
    if (selectedId.value === id) { stop(id); selectedId.value = null }
    if (queryJob.value && queryJob.value.id === id) queryJob.value = null
    await refreshJobs()
  } catch (e) {
    message.error('删除失败: ' + e.message)
  }
}

async function selectJob(id) {
  if (selectedId.value === id) { stop(id); selectedId.value = null; return }
  selectedId.value = id
  startPolling(id)
}

// ============================== OSS 数据下载 ==============================
const ossText = ref('')
const ossOwner = ref('')          // 等级负责人（必填）
const ossDesc = ref('')           // 数据情况说明（必填）
const ossTask = ref(null)
const ossLog = ref('')
const ossBusy = ref(false)
const ossComplete = ref(false)   // OSS 下载刚完成，用于展示"创建并运行"联动
let ossTimer = null
let ossPubJwk = null

function b64u(buf) {
  let s = ''
  const u = buf instanceof ArrayBuffer ? new Uint8Array(buf) : new Uint8Array(buf)
  for (const b of u) s += String.fromCharCode(b)
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function b64uFromBin(bin) {
  // node-forge 输出的二进制字符串 → b64url
  let s = ''
  for (let i = 0; i < bin.length; i++) s += String.fromCharCode(bin.charCodeAt(i))
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function b64uToStr(s) {
  s += '='.repeat((4 - (s.length % 4)) % 4)
  return atob(s.replace(/-/g, '+').replace(/_/g, '/'))
}

function bytesFromB64u(s) {
  const bin = b64uToStr(s)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

function _b64uToHex(s) {
  return Array.from(b64uToStr(s), (c) => c.charCodeAt(0).toString(16).padStart(2, '0')).join('')
}

function forgeEnvelopeEncrypt(text, jwk) {
  // AES-256-GCM（输出 ct||tag，与 WebCrypto 规范一致）
  const key = forge.random.getBytesSync(32)
  const iv = forge.random.getBytesSync(12)
  const cipher = forge.cipher.createCipher('AES-GCM', key)
  cipher.start({ iv })
  cipher.update(forge.util.createBuffer(text, 'utf8'))
  cipher.finish()
  const data = cipher.output.getBytes() + cipher.mode.tag.getBytes()
  // RSA-OAEP-SHA256 加密 AES 密钥的 JWK JSON（与 WebCrypto 路径结构一致，后端只读 k 字段）
  const n = new forge.jsbn.BigInteger(_b64uToHex(jwk.n), 16)
  const e = new forge.jsbn.BigInteger(_b64uToHex(jwk.e), 16)
  const pub = forge.pki.rsa.setPublicKey(n, e)
  const aesJwk = JSON.stringify({ kty: 'oct', k: b64uFromBin(key) })
  const ek = pub.encrypt(aesJwk, 'RSA-OAEP', { md: forge.sha256.create() })
  return { ek: b64uFromBin(ek), iv: b64uFromBin(iv), data: b64uFromBin(data) }
}

async function encryptOssInfo(text) {
  const jwk = ossPubJwk || (ossPubJwk = await api.ossPubkey())
  if (!(window.crypto && window.crypto.subtle)) {
    // 非安全上下文（http://内网IP）降级：node-forge 纯 JS 实现
    return forgeEnvelopeEncrypt(text, jwk)
  }
  const pub = await crypto.subtle.importKey('jwk', jwk, { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['encrypt'])
  const aes = await crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, true, ['encrypt', 'decrypt'])
  const iv = crypto.getRandomValues(new Uint8Array(12))
  const data = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, aes, new TextEncoder().encode(text))
  const aesJwk = await crypto.subtle.exportKey('jwk', aes)
  const ek = await crypto.subtle.encrypt({ name: 'RSA-OAEP', pub }, new TextEncoder().encode(JSON.stringify(aesJwk)))
  return { ek: b64u(ek), iv: b64u(iv), data: b64u(data) }
}

async function startOssDownload() {
  const text = (ossText.value || '').trim()
  if (!text) { message.warning('请先粘贴阿里云交付信息（4 行）'); return }
  if (!(ossOwner.value || '').trim()) { message.warning('请填写等级负责人'); return }
  if (!(ossDesc.value || '').trim()) { message.warning('请填写数据情况说明'); return }
  ossBusy.value = true
  try {
    const payload = await encryptOssInfo(text)
    payload.owner = ossOwner.value.trim()
    payload.description = ossDesc.value.trim()
    // 下载基座固定为数据根目录（后端取 config.ini [data] runs_root），勿传检测出的 fastq 父级
    ossTask.value = await api.ossDownload(payload)
    const action = ossTask.value.action
    if (action === 'running') {
      message.info('相同交付信息的任务正在下载中，未重复启动；登记信息已更新')
    } else if (action === 'done') {
      message.info('该交付信息的数据此前已下载完成，未重复下载；登记信息已更新')
    } else if (action === 'resumed') {
      message.success('检测到中断/失败任务，已复用同一任务续传（自动跳过已下载文件）')
    } else {
      message.success('下载任务已创建')
    }
    ossComplete.value = (action === 'done')   // 数据已就绪，直接亮起「创建并运行」
    if (ossTimer) clearInterval(ossTimer)
    ossLog.value = ''
    ossTimer = setInterval(pollOss, 2000)
  } catch (e) {
    message.error('提交失败: ' + e.message)
  } finally {
    ossBusy.value = false
  }
}

async function pollOss() {
  const t = ossTask.value
  if (!t) return
  try {
    ossTask.value = await api.ossTask(t.id)
    ossLog.value = await api.ossLog(t.id, 100)
    if (ossTask.value.finished && ossTimer) {
      clearInterval(ossTimer); ossTimer = null
      if (!ossComplete.value) {
        ossComplete.value = true
        message.success('数据下载完成，可直接基于该 raw_root 创建并运行任务')
      }
      loadOssTasks()
    }
  } catch { /* ignore */ }
}

// OSS 下载完成 → 用当前 raw_root（指向新下载数据）直接创建并运行（联动卡片①）
async function createAndRunFromDownload() {
  if (!uploadFile.value) { message.warning('请先在卡片①上传 metainfo Excel'); return }
  if (!form.value.raw_root.trim()) { message.warning('请填写 raw_root'); return }
  await createAndRun()
  activeTab.value = 'analysis'   // 切到分析任务页查看新任务进度
}

// ============================== OSS 下载任务查询（任务ID + 进度展示/查询） ==============================
const ossTaskList = ref([])        // 全部 OSS 下载任务
// run 文件夹名 → OSS 登记信息（负责人/数据情况说明）；同 run 多任务时以后创建的为准
const runMetaMap = computed(() => {
  const map = {}
  for (const t of ossTaskList.value) {
    const name = String(t.oss_path || '').replace(/\/+$/, '').split('/').pop()
    if (name) map[name] = { owner: t.owner || '', desc: t.description || '' }
  }
  return map
})
const ossQueryId = ref('')         // 待查询的 OSS 任务 ID
const ossQueryTask = ref(null)     // 查询到的 OSS 任务详情
const ossQueryLoading = ref(false)
let ossQueryTimer = null

async function loadOssTasks() {
  try { ossTaskList.value = await api.ossTaskList() } catch { /* ignore */ }
}

async function queryOssTask() {
  const id = (ossQueryId.value || '').trim()
  if (!id) { message.warning('请输入 OSS 任务 ID，如 oss_1790123456'); return }
  ossQueryLoading.value = true
  try {
    ossQueryTask.value = await api.ossTask(id)
    if (ossQueryTimer) clearInterval(ossQueryTimer)
    if (!ossQueryTask.value.finished) ossQueryTimer = setInterval(pollOssTask, 2000)
    else if (ossQueryTimer) { clearInterval(ossQueryTimer); ossQueryTimer = null }
  } catch (e) {
    ossQueryTask.value = null
    message.error('查询失败: ' + e.message)
  } finally {
    ossQueryLoading.value = false
  }
}

async function pollOssTask() {
  const t = ossQueryTask.value
  if (!t) return
  try {
    ossQueryTask.value = await api.ossTask(t.id)
    if (ossQueryTask.value.finished && ossQueryTimer) { clearInterval(ossQueryTimer); ossQueryTimer = null }
  } catch { /* ignore */ }
}

function queryOssTaskFromList(id) {
  ossQueryId.value = id
  queryOssTask()
}

function downloadOssLog(id) {
  const a = document.createElement('a')
  a.href = api.ossLogUrl(id)
  a.download = `${id}_download.log`
  a.click()
}

async function deleteOssTask(id) {
  try {
    await api.ossRemove(id)
    message.success(`已删除 OSS 任务 ${id}（任务记录/日志已删，已下载的数据目录保留）`)
    if (ossTask.value && ossTask.value.id === id) ossTask.value = null
    if (ossQueryTask.value && ossQueryTask.value.id === id) ossQueryTask.value = null
    await loadOssTasks()
  } catch (e) {
    message.error('删除失败: ' + e.message)
  }
}

// ============================== 任务查询 / 下载 ==============================
const queryId = ref('')
const queryJob = ref(null)
const queryLoading = ref(false)
let queryTimer = null

async function queryJobById() {
  const id = (queryId.value || '').trim()
  if (!id) { message.warning('请输入任务 ID（如 job_1790128521）'); return }
  queryLoading.value = true
  try {
    queryJob.value = await api.get(id)
    if (queryTimer) clearInterval(queryTimer)
    if (!queryJob.value.finished) queryTimer = setInterval(pollQueryJob, 2000)
    else if (queryTimer) { clearInterval(queryTimer); queryTimer = null }
  } catch (e) {
    queryJob.value = null
    message.error('查询失败: ' + e.message)
  } finally {
    queryLoading.value = false
  }
}

async function pollQueryJob() {
  const id = (queryId.value || '').trim()
  if (!id) return
  try {
    queryJob.value = await api.get(id)
    if (queryJob.value.finished && queryTimer) { clearInterval(queryTimer); queryTimer = null }
  } catch { /* ignore */ }
}

onUnmounted(() => { if (ossTimer) clearInterval(ossTimer); if (queryTimer) clearInterval(queryTimer); if (ossQueryTimer) clearInterval(ossQueryTimer) })

onMounted(async () => {
  await loadRuns()
  await refreshJobs()
  await loadOssTasks()
})
onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })
</script>

<template>
  <NFlex :size="16" :align="'start'" vertical style="padding: 16px; max-width: 1400px; margin: 0 auto">
    <NText tag="h1" :depth="3">CRISPResso2 分析平台</NText>

    <NTabs type="line" animated :default-value="activeTab" @update:value="activeTab = $event" style="width: 100%">
      <!-- ============ 分析任务（上传 / 任务列表 / 进度+日志） ============ -->
      <NTabPane name="analysis" tab="分析任务">
        <NGrid :cols="2" :x-gap="16" :y-gap="16" responsive="screen">
          <NGridItem>
          <NCard title="① 上传 metainfo & 配置" size="small">
        <NFlex vertical>
          <NFormItem label="Excel 文件">
            <NUpload :auto-upload="false" :multiple="false" @change="onFileChange" :show-upload-list="false">
              <NButton :loading="uploading">选择文件{{ uploadFile ? `: ${uploadFile.name}` : '' }}</NButton>
            </NUpload>
            <a href="/sample_meta.xlsx" download="sample_meta.xlsx"
               style="display:inline-block; margin-top:8px; font-size:12px">↓ 下载 yw4 示例 Excel（列结构参照）</a>
            <NText :depth="3" size="small" style="display:block; margin-top:6px">
              必需列：样品名称、ID、B5、B7、spacer、产物（no adapter）；上传后自动校验列结构。
            </NText>
          </NFormItem>

          <NFormItem label="Sheet（可多选，默认自动通过校验的 sheet）">
            <NSelect :value="form.sheet" @update:value="form.sheet=$event" multiple clearable filterable :options="sheetOptions" placeholder="上传后自动列出，可多选" />
            <template v-if="Object.keys(sheetErrors).length">
              <NFlex vertical style="margin-top:6px">
                <NText type="error" size="small" strong>以下 sheet 未通过校验（已自动排除）：</NText>
                <NTag v-for="(errs, s) in sheetErrors" :key="s" type="warning" style="margin:2px 0; max-width:520px; word-break:break-all">
                  {{ s }}：{{ errs.join('；') }}
                </NTag>
              </NFlex>
            </template>
          </NFormItem>

          <NFormItem label="原始 fastq 目录（选择 run 文件夹，自动定位 fastq 父级）">
            <NFlex vertical :gap="4" style="width: 100%">
              <NSelect
                :value="selectedRun"
                @update:value="onSelectRun"
                :options="runsOptions"
                :render-label="renderRunLabel"
                :consistent-menu-width="false"
                :dropdown-style="{ maxWidth: 'min(880px, 90vw)' }"
                :loading="detecting"
                placeholder="选择数据根目录下的 run 文件夹"
                clearable
                filterable
              />
              <NFlex v-if="selectedRun" align="flex-start" :gap="6" style="flex-wrap:wrap">
                <NText :depth="3" size="small" style="white-space:nowrap">已选 run：</NText>
                <NText size="small" style="font-family:monospace; word-break:break-all; white-space:normal; flex:1 1 auto; min-width:0">{{ selectedRunName }}</NText>
                <NText v-if="selectedRunMeta && (selectedRunMeta.owner || selectedRunMeta.desc)"
                  :depth="3" size="small" style="flex-basis:100%; word-break:break-all">
                  负责人: {{ selectedRunMeta.owner || '-' }} · 数据情况说明: {{ selectedRunMeta.desc || '-' }}
                </NText>
              </NFlex>
              <NFlex v-if="lastDetect" align="center" :gap="6" style="margin-top:2px; flex-wrap:wrap">
                <NTag type="success" size="small">已定位 fastq 父级（{{ lastDetect.samples }} 个样本子目录）</NTag>
              </NFlex>
            </NFlex>
          </NFormItem>

          <NFlex :cols="2" :gap="12">
            <NFormItem label="scores（可多选）">
              <NSelect :value="form.scores" @update:value="form.scores=$event" multiple clearable filterable :options="scoreOptions" placeholder="选择要分析的 score" />
            </NFormItem>
            <NFormItem label="并行 workers">
              <NInputNumber :value="form.workers" @update:value="form.workers=$event" min="1" max="256" step="1" :show-status="false" style="width: 100%" />
            </NFormItem>
          </NFlex>

          <NFormItem label="CRISPResso 对齐/定量参数（留空 = CRISPResso2 默认）">
            <NFlex vertical :gap="6" style="width: 100%">
              <NFlex :gap="12" wrap>
                <NFormItem label="qw_center（默认 -3）" label-placement="left" size="small" :show-feedback="false">
                  <NInputNumber :value="form.quantification_window_center" @update:value="form.quantification_window_center=$event"
                    placeholder="默认 -3" :show-status="false" style="width: 130px" />
                </NFormItem>
                <NFormItem label="qw_size（默认 1）" label-placement="left" size="small" :show-feedback="false">
                  <NInputNumber :value="form.quantification_window_size" @update:value="form.quantification_window_size=$event"
                    placeholder="默认 1" min="1" :show-status="false" style="width: 130px" />
                </NFormItem>
                <NFormItem label="nw_gap_open（默认 -20）" label-placement="left" size="small" :show-feedback="false">
                  <NInputNumber :value="form.needleman_wunsch_gap_open" @update:value="form.needleman_wunsch_gap_open=$event"
                    placeholder="默认 -20" :show-status="false" style="width: 130px" />
                </NFormItem>
              </NFlex>
              <NFlex :gap="8" align="center">
                <NButton size="small" type="primary" secondary @click="applyPreset('Cas9')">Cas9 参数一键填充</NButton>
                <NButton size="small" type="primary" secondary @click="applyPreset('Cas12f1')">Cas12f1 参数一键填充</NButton>
                <NButton size="small" type="primary" secondary @click="applyPreset('IsCB')">IsCB 参数一键填充</NButton>
                <NButton size="small" quaternary @click="resetCrispressoDefaults">重置为 CRISPResso2 默认</NButton>
                <NText :depth="3" size="small">
                  预设：Cas9 qw_center=-3 / qw_size=10；Cas12f1 qw_center=+1 / qw_size=10；IsCB qw_center=-9 / qw_size=6
                </NText>
              </NFlex>
            </NFlex>
          </NFormItem>

          <NFormItem label="选项">
            <NFlex vertical :gap="6" style="width: 100%">
              <NCheckbox v-model="form.force">强制重跑（覆盖断点续跑）</NCheckbox>
              <NFlex align="center" :gap="8">
                <NSwitch :value="form.ignore_substitutions" @update:value="form.ignore_substitutions=$event" size="small" />
                <NText size="small">忽略替换（--ignore_substitutions，编辑效率仅统计 indel，可过滤错配污染导致的假阳性）</NText>
              </NFlex>
            </NFlex>
          </NFormItem>

          <NFormItem label="额外通知邮箱（可选）" :show-feedback="false">
            <NInput :value="form.notify_email" @update:value="form.notify_email=$event"
              placeholder="任务完成后结果邮件追加发送给该邮箱；多个用逗号分隔。留空只发 zhangyiming@ihcams.ac.cn"
              clearable />
          </NFormItem>

          <NButton type="primary" size="large" @click="createAndRun" block>创建并运行全流程</NButton>
        </NFlex>
      </NCard>
      </NGridItem>

      <NGridItem>
      <NFlex vertical :gap="16">
      <NCard title="② 任务列表" size="small">
          <NFlex vertical :gap="8">
          <NButton v-if="!jobs.length" disabled>暂无任务</NButton>
            <NCard v-for="j in jobs" :key="j.id" size="small" :bordered="j.id === selectedId" :interactive="false"
              @click="selectJob(j.id)">
            <NFlex align="center">
              <NText strong>{{ j.id }}</NText>
              <NTag v-if="j.finished" type="success" size="small">已完成</NTag>
              <NTag v-else-if="Object.values(j.steps).some(s => s === 'running')" type="warning" size="small">运行中</NTag>
              <NTag v-else type="info" size="small">等待中</NTag>
              <NText :depth="3" size="small">{{ j.cfg ? j.cfg.batch : '' }} · {{ j.cfg ? j.cfg.scores.join(',') : '' }}</NText>
              <NPopconfirm v-if="j.finished || Object.values(j.steps).every(s => s === 'pending')"
                @positive-click="() => deleteJob(j.id)">
                <template #trigger>
                  <NButton size="tiny" type="error" quaternary @click.stop>删除</NButton>
                </template>
                删除任务 {{ j.id }}？独占产物将被释放，仍被其他任务引用的共享缓存会保留。
              </NPopconfirm>
            </NFlex>
          </NCard>
        </NFlex>
      </NCard>
      <NCard :title="selectedJob ? `③ 进度 + 日志 · ${selectedJob.id}` : '③ 任务详情'" size="small" :block="false">
        <NFlex v-if="!selectedJob" vertical>
          <NEmpty description="左侧选择任务查看详情" />
        </NFlex>
        <NFlex v-else vertical :gap="12">
          <NSteps :current="0" status="finish">
            <!-- 用分步卡片更直观 -->
          </NSteps>
          <NFlex :gap="8" wrap>
            <NCard v-for="s in ['config','amplicons','split','crispresso','collect','pack']" :key="s" size="small" :title="STEP_LABEL[s]"
              :bordered="false" :content-style="{ padding: '8px 12px' }">
              <NFlex align="center" :gap="6">
                <NTag :type="{ pending:'info', running:'warning', done:'success', failed:'error' }[job.steps[s]]" size="small">
                  {{ { pending:'等待', running:'运行中', done:'已完成', failed:'失败' }[job.steps[s]] }}
                </NTag>
                <NText v-if="job.error[s] && job.steps[s] === 'failed'" :depth="3" size="small" style="max-width: 220px" title="click">{{ job.error[s] }}</NText>
              </NFlex>
            </NCard>
          </NFlex>

          <NFlex v-if="job.cache && job.cache.enabled" align="center" :gap="6" wrap>
            <NTag size="small" type="info" :bordered="false">跨任务缓存</NTag>
            <NText :depth="3" size="small">
              split: {{ job.cache.split }} · crispresso: {{ job.cache.crispresso }}
            </NText>
          </NFlex>

          <NFlex :gap="8" wrap>
            <NButton v-if="!job.finished || hasFailedStep" :type="hasFailedStep ? 'warning' : 'primary'" size="small"
              @click="() => api.start(selectedJob.id)">
              {{ hasFailedStep ? '重新运行（补跑失败步骤）' : '重新运行全部' }}
            </NButton>
            <NButton size="small" @click="() => download('csv', selectedJob.id)" :disabled="job.steps.collect !== 'done'">下载效率明细</NButton>
            <NButton size="small" @click="() => download('xlsx', selectedJob.id)" :disabled="job.steps.collect !== 'done'">下载效率 Excel</NButton>
            <NButton type="success" size="small" @click="() => download('pack', selectedJob.id)" :disabled="job.steps.pack !== 'done'">下载打包 zip</NButton>
            <NButton v-if="hasFailedStep" type="warning" size="small" @click="() => downloadLog(selectedJob.id)">下载错误日志（run.log）</NButton>
          </NFlex>

          <NCard title="运行日志" size="small" style="width: 100%">
            <pre style="margin:0; max-height: 360px; overflow:auto; font-size: 12px; white-space: pre-wrap; word-break: break-all">{{ logText || '等待输出…' }}</pre>
          </NCard>
        </NFlex>
      </NCard>
      </NFlex>
      </NGridItem>
        </NGrid>
      </NTabPane>

      <!-- ============ OSS 数据下载（放大，独立整页） ============ -->
      <NTabPane name="oss" tab="OSS 数据下载">
        <NCard title="④ OSS 数据下载" size="medium" style="max-width: 920px; margin: 0 auto">
          <NFlex vertical :gap="8">
          <NText :depth="3" size="small">
            粘贴阿里云交付信息（4 行），后端自动登记 ossutil 配置并下载数据到 raw_root；凭据经 RSA+AES 加密传输，不落明文。
          </NText>
          <NFormItem label="等级负责人（必填）" :show-feedback="false">
            <NInput v-model:value="ossOwner" placeholder="填写本批数据等级的负责人姓名" clearable />
          </NFormItem>
          <NFormItem label="数据情况说明（必填）" :show-feedback="false">
            <NInput v-model:value="ossDesc" type="textarea" :rows="2"
              placeholder="简要说明数据来源、用途/项目、数据等级等（将随下载日志留痕）" />
          </NFormItem>
          <NInput v-model:value="ossText" type="textarea" :rows="5"
            placeholder="阿里云交付路径：oss://bucket/任务名&#10;accessKeyID：xxx&#10;accessKeySecret：xxx&#10;oss_endpoint：http://oss-cn-xxx.aliyuncs.com" />
          <NButton type="primary" :loading="ossBusy" @click="startOssDownload" block>解密并开始下载</NButton>
          <template v-if="ossTask">
            <NFlex align="center" :gap="8" wrap>
              <NText strong>{{ ossTask.id }}</NText>
              <NTag :type="{ running:'warning', done:'success', failed:'error' }[ossTask.status]" size="small">
                {{ { running:'下载中', done:'完成', failed:'失败' }[ossTask.status] }}
              </NTag>
              <NText :depth="3" size="small" style="word-break:break-all">目标：{{ ossTask.target }}</NText>
            </NFlex>
            <NFlex align="center" :gap="6" wrap>
              <NTag size="small" type="info" :bordered="false">等级负责人</NTag>
              <NText size="small">{{ ossTask.owner || '-' }}</NText>
              <NTag size="small" type="info" :bordered="false" style="margin-left:8px">数据情况说明</NTag>
              <NText size="small" style="word-break:break-all">{{ ossTask.description || '-' }}</NText>
            </NFlex>
            <NCard title="下载日志" size="small" style="width:100%">
              <pre style="margin:0; max-height:200px; overflow:auto; font-size:12px; white-space:pre-wrap; word-break:break-all">{{ ossLog || '等待输出…' }}</pre>
            </NCard>
            <NFlex v-if="ossComplete" align="center" :gap="8" wrap style="margin-top:8px">
              <NButton type="primary" size="small" @click="createAndRunFromDownload">用刚下载的数据创建并运行</NButton>
              <NText :depth="3" size="small">数据已就位，可直接创建分析任务</NText>
            </NFlex>
          </template>

          <NDivider style="margin: 4px 0">OSS 下载任务查询</NDivider>

          <NFlex :gap="8" wrap align="center">
            <NInput v-model:value="ossQueryId" placeholder="输入 OSS 任务 ID，如 oss_1790123456" clearable style="width: 240px" />
            <NButton type="primary" :loading="ossQueryLoading" @click="queryOssTask">查询</NButton>
            <NButton @click="loadOssTasks">刷新列表</NButton>
          </NFlex>

          <!-- 查询结果：任务ID + 进度展示 -->
          <template v-if="ossQueryTask">
            <NFlex align="center" :gap="6" wrap>
              <NText strong>{{ ossQueryTask.id }}</NText>
              <NTag :type="{ running:'warning', done:'success', failed:'error' }[ossQueryTask.status]" size="small">
                {{ { running:'下载中', done:'完成', failed:'失败' }[ossQueryTask.status] }}
              </NTag>
              <NText :depth="3" size="small" style="word-break:break-all">目标：{{ ossQueryTask.target }}</NText>
            </NFlex>
            <NFlex v-if="ossQueryTask.progress" vertical :gap="2" style="width: 100%">
              <NProgress type="line" :height="10" :percentage="Math.floor(ossQueryTask.progress.percent || 0)"
                :status="ossQueryTask.status === 'failed' ? 'error' : (ossQueryTask.status === 'done' ? 'success' : 'default')" />
              <NText :depth="3" size="small" style="word-break:break-all">
                <template v-if="ossQueryTask.progress.text">{{ ossQueryTask.progress.text }}</template>
                <template v-else>
                  已下 {{ ossQueryTask.progress.done || '-' }} / 总 {{ ossQueryTask.progress.total || '-' }}
                  <template v-if="ossQueryTask.progress.skipped"> · 跳过 {{ ossQueryTask.progress.skipped }}</template>
                  · 速度 {{ ossQueryTask.progress.speed || '-' }} · {{ ossQueryTask.progress.percent }}%
                </template>
              </NText>
            </NFlex>
            <NText v-else-if="ossQueryTask.status === 'running'" :depth="3" size="small">
              正在等待 ossutil 输出进度…
            </NText>
            <NFlex align="center" :gap="6" wrap>
              <NTag size="small" type="info" :bordered="false">等级负责人</NTag>
              <NText size="small">{{ ossQueryTask.owner || '-' }}</NText>
              <NTag size="small" type="info" :bordered="false" style="margin-left:8px">数据情况说明</NTag>
              <NText size="small" style="word-break:break-all">{{ ossQueryTask.description || '-' }}</NText>
            </NFlex>
            <NButton size="small" type="warning" @click="downloadOssLog(ossQueryTask.id)">下载下载日志</NButton>
            <NPopconfirm @positive-click="() => deleteOssTask(ossQueryTask.id)">
              <template #trigger>
                <NButton size="small" type="error" quaternary>删除任务</NButton>
              </template>
              删除任务 {{ ossQueryTask.id }}？任务记录与日志将删除，已下载的数据目录保留。
            </NPopconfirm>
          </template>

          <!-- 任务列表 -->
          <NText depth="3" size="small">最近下载任务（点击查看详情）：</NText>
          <NFlex vertical :gap="6" style="max-height: 340px; overflow:auto">
            <NCard v-for="t in ossTaskList" :key="t.id" size="small" :bordered="false" :interactive="false" style="width:100%"
              @click="queryOssTaskFromList(t.id)">
              <NFlex align="center" :gap="8" wrap>
                <NText strong style="font-family:monospace; word-break:break-all">{{ t.id }}</NText>
                <NTag :type="{ running:'warning', done:'success', failed:'error' }[t.status]" size="small">
                  {{ { running:'下载中', done:'完成', failed:'失败' }[t.status] }}
                </NTag>
                <NText :depth="3" size="small" style="word-break:break-all">{{ t.target }}</NText>
                <NTag v-if="t.owner" size="tiny" :bordered="false">负责人: {{ t.owner }}</NTag>
                <NText v-if="t.description" :depth="3" size="small" style="word-break:break-all; flex-basis:100%">
                  备注: {{ t.description }}
                </NText>
                <NText v-if="t.progress && t.progress.percent != null && t.status === 'running'"
                  :depth="3" size="small">{{ Math.floor(t.progress.percent) }}%</NText>
                <NPopconfirm @positive-click="() => deleteOssTask(t.id)">
                  <template #trigger>
                    <NButton size="tiny" type="error" quaternary @click.stop>删除</NButton>
                  </template>
                  删除任务 {{ t.id }}？任务记录与日志将删除，已下载的数据目录保留。
                </NPopconfirm>
              </NFlex>
            </NCard>
            <NEmpty v-if="!ossTaskList.length" description="暂无下载任务" style="width:100%" />
          </NFlex>
        </NFlex>
        </NCard>
      </NTabPane>

      <!-- ============ 任务查询 / 下载（放大，独立整页） ============ -->
      <NTabPane name="query" tab="任务查询">
        <NCard title="⑤ 任务查询 / 下载" size="medium" style="max-width: 920px; margin: 0 auto">
          <NFlex vertical :gap="10">
          <NFlex :gap="8" align="center">
            <NInput v-model:value="queryId" placeholder="输入任务 ID，如 job_1790128521" clearable @keyup.enter="queryJobById" />
            <NButton type="primary" :loading="queryLoading" @click="queryJobById">查询</NButton>
          </NFlex>
          <template v-if="queryJob">
            <NFlex align="center" :gap="6" wrap>
              <NText strong>{{ queryJob.id }}</NText>
              <NTag v-if="queryJob.from_disk" size="small" :bordered="false">历史任务</NTag>
              <NTag v-if="queryJob.finished" type="success" size="small">已完成</NTag>
              <NTag v-else type="warning" size="small">进行中（自动刷新）</NTag>
            </NFlex>
            <NFlex :gap="6" wrap>
              <NTag v-for="s in ['config','amplicons','split','crispresso','collect','pack']" :key="s"
                :type="{ pending:'info', running:'warning', done:'success', failed:'error' }[queryJob.steps[s]]" size="small">
                {{ STEP_LABEL[s] }}·{{ { pending:'等待', running:'运行中', done:'完成', failed:'失败' }[queryJob.steps[s]] }}
              </NTag>
            </NFlex>
            <NFlex :gap="8" wrap>
              <NButton type="success" size="small" @click="() => download('pack', queryJob.id)">下载打包 zip</NButton>
              <NButton size="small" @click="() => download('csv', queryJob.id)">效率 CSV</NButton>
             <NButton size="small" @click="() => download('xlsx', queryJob.id)">效率 Excel</NButton>
               <NButton v-if="queryHasFailed" type="warning" size="small" @click="() => downloadLog(queryJob.id)">下载错误日志（run.log）</NButton>
              <NPopconfirm @positive-click="() => deleteJob(queryJob.id)">
                <template #trigger>
                  <NButton size="small" type="error" quaternary>删除任务</NButton>
                </template>
                删除任务 {{ queryJob.id }}？独占产物将被释放，仍被其他任务引用的共享缓存会保留。
              </NPopconfirm>
             </NFlex>
          </template>
          <NEmpty v-else description="输入任务名称/ID 查询进度并下载数据" />
        </NFlex>
      </NCard>
      </NTabPane>
    </NTabs>
      </NFlex>
</template>
