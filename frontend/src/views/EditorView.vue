<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
import { store } from '../store'

const props = defineProps({ id: { type: [String, Number], required: true } })
const router = useRouter()

const project = ref(null)
const selected = ref(0)
const err = ref('')
const saving = ref(false)
const refInfo = ref({})        // ref string -> { url, filename }
const job = ref(null)          // whole-film job
const shotJobs = ref({})       // shot id -> job (单镜生成)
const comfy = ref(null)
let timer = null
let comfyTimer = null

// ---- 镜头草稿:编辑只改本地,必须点「保存」才落库 ----
const draft = ref(null)
const dirty = ref(false)

// ---- AI 优化提示词 ----
const optimizeAsk = ref('')
const optimizing = ref(false)
const optimizeMsg = ref('')

// ---- 素材管理 ----
const CATS = [
  { v: 'character', t: '人物' },
  { v: 'prop', t: '道具' },
  { v: 'scene', t: '场景' }
]
const assets = ref([])
const assetTab = ref('character')
const showAssets = ref(false)
const dragOver = ref(false)
const promptEl = ref(null)
let dragged = false

const MODES = [
  { value: 'ref', label: '参考图生视频', hint: '把参考图整体喂给模型,保持人物/服装一致(可多张)' },
  { value: 'first', label: '首帧生视频', hint: '用第 1 张参考图作为首帧' },
  { value: 'first_last', label: '首尾帧生视频', hint: '第 1 张=首帧,第 2 张=尾帧' },
  { value: 'text', label: '纯文本生视频', hint: '不使用参考图' }
]

const shot = computed(() => (project.value ? project.value.shots[selected.value] : null))
const fps = computed(() => (project.value ? project.value.fps : 24))
const running = computed(() => job.value && ['queued', 'running'].includes(job.value.status))
const modeHint = computed(() => (MODES.find((m) => m.value === draft.value?.mode) || {}).hint || '')
const shotJob = computed(() => (shot.value ? shotJobs.value[shot.value.id] || null : null))
const shotBusy = computed(() => shotJob.value && ['queued', 'running'].includes(shotJob.value.status))
const anyBusy = computed(
  () => running.value || Object.values(shotJobs.value).some((j) => ['queued', 'running'].includes(j.status))
)

// ---- 成片设置:分辨率 / 帧率(改完必须点「保存设置」) ----
const RES_PRESETS = [
  { v: '1080x1920', t: '竖屏 1080×1920 (9:16)' },
  { v: '720x1280', t: '竖屏 720×1280 (9:16)' },
  { v: '1080x1350', t: '竖屏 1080×1350 (4:5)' },
  { v: '1080x1080', t: '方形 1080×1080 (1:1)' },
  { v: '1920x1080', t: '横屏 1920×1080 (16:9)' },
  { v: 'custom', t: '自定义…' }
]
const FPS_OPTIONS = [12, 16, 24, 25, 30, 60]
const cfg = reactive({ preset: '1080x1920', w: 1080, h: 1920, fps: 24 })
const cfgDirty = computed(() => {
  const p = project.value
  if (!p) return false
  return Number(cfg.w) !== p.out_width || Number(cfg.h) !== p.out_height || Number(cfg.fps) !== p.fps
})

function resetDraft() {
  const s = shot.value
  draft.value = s
    ? {
        title: s.title || '',
        mode: s.mode,
        sec: Number((s.duration_frames / project.value.fps).toFixed(2)),
        prompt: s.prompt || '',
        refs: [...(s.refs || [])],
        options: JSON.parse(JSON.stringify(s.options || {})),
        enabled: s.enabled
      }
    : null
  dirty.value = false
}

function snapshot(s) {
  return JSON.stringify({
    title: s.title || '',
    mode: s.mode,
    sec: Number((s.duration_frames / project.value.fps).toFixed(2)),
    prompt: s.prompt || '',
    refs: s.refs || [],
    options: s.options || {},
    enabled: s.enabled
  })
}

function markDirty() {
  dirty.value = !!shot.value && !!draft.value && snapshot(shot.value) !== JSON.stringify(draft.value)
}

watch(selected, () => resetDraft())

async function load() {
  err.value = ''
  try {
    project.value = await api.project(props.id)
    const all = new Set()
    for (const s of project.value.shots) for (const r of s.refs || []) all.add(r)
    for (const r of all) refInfo.value[r] = await store.resolveRef(r)
    job.value = project.value.latest_job
    syncConfig()
    resetDraft()
  } catch (e) {
    err.value = e.message
  }
}

async function refreshComfy() {
  try {
    comfy.value = await api.comfyHealth()
  } catch (e) {
    comfy.value = { up: false }
  }
}

onMounted(async () => {
  await store.bootstrap()
  await load()
  await refreshComfy()
  timer = setInterval(async () => {
    if (job.value && ['queued', 'running'].includes(job.value.status)) {
      try {
        job.value = await api.job(job.value.id)
        if (!['queued', 'running'].includes(job.value.status)) await refreshShots()
      } catch (e) {
        /* transient */
      }
    }
    const active = Object.entries(shotJobs.value).filter(([, j]) => ['queued', 'running'].includes(j.status))
    for (const [sid, j] of active) {
      try {
        const updated = await api.job(j.id)
        shotJobs.value = { ...shotJobs.value, [sid]: updated }
        if (!['queued', 'running'].includes(updated.status)) await refreshShots()
      } catch (e) {
        /* transient */
      }
    }
  }, 3000)
  comfyTimer = setInterval(refreshComfy, 15000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
  if (comfyTimer) clearInterval(comfyTimer)
})

/** 只刷新镜头状态与预览片段,不动用户正在编辑的草稿 */
async function refreshShots() {
  const p = await api.project(props.id)
  for (const s of p.shots) {
    const cur = project.value.shots.find((x) => x.id === s.id)
    if (cur) {
      cur.status = s.status
      cur.clip_url = s.clip_url
    }
  }
  project.value.status = p.status
  project.value.final_url = p.final_url
}

async function saveDraft() {
  if (!shot.value || !draft.value) return
  saving.value = true
  err.value = ''
  try {
    const updated = await api.updateShot(project.value.id, shot.value.id, {
      title: draft.value.title,
      mode: draft.value.mode,
      duration_frames: Math.max(4, Math.round(Number(draft.value.sec) * project.value.fps)),
      prompt: draft.value.prompt,
      refs: draft.value.refs,
      options: draft.value.options,
      enabled: draft.value.enabled
    })
    Object.assign(shot.value, updated)
    resetDraft()
  } catch (e) {
    err.value = e.message
  } finally {
    saving.value = false
  }
}

function revertDraft() {
  resetDraft()
}

/** 让模型优化当前草稿里的提示词(只写回编辑框,不落库) */
async function optimizePrompt() {
  if (!draft.value) return
  err.value = ''
  optimizeMsg.value = ''
  if (!draft.value.prompt.trim()) {
    err.value = '提示词是空的,没什么可优化'
    return
  }
  optimizing.value = true
  try {
    const res = await api.optimizeShot(project.value.id, shot.value.id, {
      prompt: draft.value.prompt,
      instruction: optimizeAsk.value
    })
    draft.value.prompt = res.prompt
    markDirty()
    optimizeMsg.value = `已由 ${res.engine_detail} 优化,确认没问题后点「保存」生效;不满意可以直接改回或再优化一次。`
  } catch (e) {
    err.value = e.message
  } finally {
    optimizing.value = false
  }
}

function selectShot(i) {
  if (i === selected.value) return
  if (dirty.value && !confirm('当前镜头有未保存的修改,切换会丢失。确定切换吗?')) return
  selected.value = i
}

async function deleteShot() {
  if (!shot.value) return
  if (!confirm(`删除镜头 ${shot.value.idx}(${shot.value.title || shot.value.key})?此操作不可撤销。`)) return
  err.value = ''
  try {
    await api.deleteShot(project.value.id, shot.value.id)
    await load()
  } catch (e) {
    err.value = e.message
  }
}

/* ---------- 单镜手动生成(可反复重跑) ---------- */
async function renderShot() {
  if (!shot.value) return
  err.value = ''
  if (dirty.value) {
    err.value = '当前镜头有未保存的修改,请先点「保存」再生成'
    return
  }
  try {
    const j = await api.renderShot(project.value.id, shot.value.id)
    shotJobs.value = { ...shotJobs.value, [shot.value.id]: j }
  } catch (e) {
    err.value = e.message
  }
}

async function cancelShot() {
  const j = shotJob.value
  if (!j) return
  try {
    const r = await api.cancelJob(j.id)
    shotJobs.value = { ...shotJobs.value, [shot.value.id]: r }
  } catch (e) {
    err.value = e.message
  }
}

async function addRef(event) {
  const file = event.target.files && event.target.files[0]
  event.target.value = ''
  if (!file || !draft.value) return
  try {
    const asset = await api.upload(file, assetTab.value)
    assets.value.unshift(asset)
    refInfo.value[`asset:${asset.id}`] = { url: asset.url, filename: asset.filename }
    draft.value.refs = [...draft.value.refs, `asset:${asset.id}`]
    markDirty()
  } catch (e) {
    err.value = e.message
  }
}

function removeRef(i) {
  if (!draft.value) return
  const refs = [...draft.value.refs]
  refs.splice(i, 1)
  // keep <Picture N> tokens in the prompt in sync with the new reference order
  draft.value.prompt = (draft.value.prompt || '').replace(/<Picture (\d+)>/g, (m, d) => {
    const n = Number(d)
    if (n === i + 1) return ''
    return n > i + 1 ? `<Picture ${n - 1}>` : m
  })
  draft.value.refs = refs
  markDirty()
}

/* ---------- 素材管理 ---------- */
function catName(v) {
  const c = CATS.find((x) => x.v === v)
  return c ? c.t : v
}

async function openAssets() {
  showAssets.value = true
  if (!assets.value.length) await loadAssets()
}

async function loadAssets() {
  try {
    assets.value = await api.myAssets()
  } catch (e) {
    err.value = e.message
  }
}

async function uploadAsset(event) {
  const file = event.target.files && event.target.files[0]
  event.target.value = ''
  if (!file) return
  try {
    assets.value.unshift(await api.upload(file, assetTab.value))
  } catch (e) {
    err.value = e.message
  }
}

async function saveAsset(a) {
  try {
    Object.assign(a, await api.updateAsset(a.id, { category: a.category, filename: a.filename }))
  } catch (e) {
    err.value = e.message
  }
}

async function deleteAsset(a) {
  if (!confirm(`删除素材「${a.filename}」?引用了它的镜头会失去这张图。`)) return
  try {
    await api.deleteAsset(a.id)
    assets.value = assets.value.filter((x) => x.id !== a.id)
  } catch (e) {
    err.value = e.message
  }
}

function dragAsset(ev, a) {
  dragged = true
  ev.dataTransfer.setData('text/plain', `asset:${a.id}`)
  ev.dataTransfer.effectAllowed = 'copy'
}

function dragEnd() {
  setTimeout(() => {
    dragged = false
  }, 180)
}

function insertAsset(a) {
  if (!dragged) insertAssetRef(`asset:${a.id}`)
}

function dropPrompt(ev) {
  dragOver.value = false
  const ref = ev.dataTransfer.getData('text/plain')
  if (ref && ref.startsWith('asset:')) insertAssetRef(ref)
}

/** 把素材加进本镜头参考图(草稿),并在光标处插入 <Picture N> 引用 */
function insertAssetRef(ref) {
  if (!draft.value) return
  const refs = [...draft.value.refs]
  let n = refs.indexOf(ref)
  if (n === -1) {
    refs.push(ref)
    n = refs.length - 1
  }
  const token = `<Picture ${n + 1}>`
  const el = promptEl.value
  const prompt = draft.value.prompt || ''
  const at = el && typeof el.selectionStart === 'number' ? el.selectionStart : prompt.length
  const end = el && typeof el.selectionEnd === 'number' ? el.selectionEnd : at
  draft.value.prompt = prompt.slice(0, at) + token + prompt.slice(end)
  draft.value.refs = refs
  if (el) {
    el.focus()
    el.selectionStart = el.selectionEnd = at + token.length
  }
  markDirty()
  if (!refInfo.value[ref]) store.resolveRef(ref).then((info) => (refInfo.value[ref] = info))
}

async function startRender() {
  err.value = ''
  if (dirty.value) {
    err.value = '有镜头存在未保存的修改,请先点「保存」再开始生成'
    return
  }
  try {
    job.value = await api.render(project.value.id)
  } catch (e) {
    err.value = e.message
  }
}

async function cancel() {
  if (!job.value) return
  job.value = await api.cancelJob(job.value.id)
}

async function saveTitle() {
  if (!project.value) return
  await api.renameProject(project.value.id, project.value.title)
}

// ---- 成片设置 ----
function syncConfig() {
  const p = project.value
  if (!p) return
  const key = `${p.out_width}x${p.out_height}`
  cfg.preset = RES_PRESETS.some((r) => r.v === key) ? key : 'custom'
  cfg.w = p.out_width
  cfg.h = p.out_height
  cfg.fps = p.fps
}

function applyPreset() {
  if (cfg.preset === 'custom') return
  const [w, h] = cfg.preset.split('x').map(Number)
  cfg.w = w
  cfg.h = h
}

async function saveConfig() {
  if (!project.value) return
  err.value = ''
  saving.value = true
  try {
    const updated = await api.updateProject(project.value.id, {
      out_width: Number(cfg.w),
      out_height: Number(cfg.h),
      fps: Number(cfg.fps)
    })
    // 后端会按新帧率重算镜头帧数,并作废旧成片 / 镜头片段
    project.value = updated
    job.value = null
    shotJobs.value = {}
    syncConfig()
    resetDraft()
  } catch (e) {
    err.value = e.message
  } finally {
    saving.value = false
  }
}

const statusText = { queued: '排队中', running: '生成中', done: '已完成', failed: '失败', canceled: '已终止' }

// ---- 另存为我的模板 ----
const showTpl = ref(false)
const tplForm = reactive({ title: '', description: '' })

function openSaveTemplate() {
  err.value = ''
  tplForm.title = `${project.value.title} · 模板`
  tplForm.description = ''
  showTpl.value = true
}

async function saveAsTemplate() {
  err.value = ''
  if (!tplForm.title.trim()) {
    err.value = '请填写模板名称'
    return
  }
  try {
    await api.saveProjectAsTemplate(project.value.id, { ...tplForm })
    showTpl.value = false
    router.push({ name: 'templates' })
  } catch (e) {
    err.value = e.message
  }
}
</script>

<template>
  <main class="page" :class="{ shifted: showAssets }">
    <div v-if="err" class="notice err" style="margin-bottom:14px">{{ err }}</div>
    <div v-if="!project" class="muted">加载中…</div>

    <template v-else>
      <div class="row" style="margin-bottom:8px">
        <input v-model="project.title" style="max-width:420px;font-size:16px;font-weight:600" @blur="saveTitle" />
        <span class="spacer" />
        <span v-if="saving" class="small muted">保存中…</span>
        <span v-if="comfy" class="badge" :class="{ email: comfy.up }">ComfyUI {{ comfy.up ? '在线' : '离线' }}</span>
        <button @click="openSaveTemplate">另存为我的模板</button>
        <button @click="openAssets">素材管理</button>
        <button v-if="!running" class="primary" :disabled="anyBusy" @click="startRender">开始生成</button>
        <button v-else class="danger" @click="cancel">终止生成</button>
      </div>

      <div class="sub">
        {{ project.shots.length }} 个镜头 · {{ project.total_frames }} 帧 · {{ project.duration_seconds }} 秒 ·
        {{ project.out_width }}×{{ project.out_height }} @ {{ project.fps }}fps
      </div>

      <div class="row wrap" style="gap:10px;align-items:center;margin:10px 0 4px">
        <span class="small muted">成片设置</span>
        <select v-model="cfg.preset" style="width:auto" :disabled="anyBusy" @change="applyPreset">
          <option v-for="r in RES_PRESETS" :key="r.v" :value="r.v">{{ r.t }}</option>
        </select>
        <template v-if="cfg.preset === 'custom'">
          <input type="number" min="128" max="4096" step="2" v-model.number="cfg.w" style="width:96px" :disabled="anyBusy" />
          <span class="muted">×</span>
          <input type="number" min="128" max="4096" step="2" v-model.number="cfg.h" style="width:96px" :disabled="anyBusy" />
        </template>
        <label class="small muted row" style="gap:6px">
          <span>帧率</span>
          <select v-model.number="cfg.fps" style="width:auto" :disabled="anyBusy">
            <option v-for="f in FPS_OPTIONS" :key="f" :value="f">{{ f }} fps</option>
          </select>
        </label>
        <span v-if="cfgDirty" class="badge" style="color:var(--warn);border-color:#5a4a1c">设置未保存</span>
        <span class="spacer" />
        <button :disabled="!cfgDirty || saving || anyBusy" @click="saveConfig">
          {{ saving ? '保存中…' : '保存设置' }}
        </button>
      </div>
      <div class="small muted" style="margin-bottom:12px">
        改分辨率或帧率会让已生成的成片与镜头片段作废,需要重新点「开始生成」。帧率越高,单镜头一次能生成的时长越短。
      </div>

      <div v-if="job" class="card" style="margin-bottom:16px">
        <div class="row" style="margin-bottom:8px">
          <strong>整片任务 #{{ job.id }}</strong>
          <span class="badge">{{ statusText[job.status] || job.status }}</span>
          <span v-if="job.current_shot" class="badge">当前 {{ job.current_shot }}</span>
          <span class="spacer" />
          <span class="small muted">{{ job.message }}</span>
        </div>
        <div class="progress"><i :style="{ width: job.progress + '%' }" /></div>
        <div v-if="job.error" class="notice err small" style="margin-top:10px">{{ job.error }}</div>
        <div v-if="job.out_url" style="margin-top:14px">
          <video :src="job.out_url" controls />
          <div class="row" style="margin-top:10px">
            <a :href="job.out_url" download><button class="ghost">下载成片</button></a>
          </div>
        </div>
      </div>

      <div class="editor">
        <!-- shot list -->
        <div class="card shot-list">
          <div v-for="(s, i) in project.shots" :key="s.id" class="shot-item" :class="{ active: i === selected }" @click="selectShot(i)">
            <span class="idx">{{ i + 1 }}</span>
            <span class="dot" :class="s.status === 'done' ? 'done' : s.status === 'running' ? 'running' : s.status === 'failed' ? 'failed' : ''" />
            <span class="small" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ s.title || s.key }}</span>
            <span class="small muted">{{ (s.duration_frames / project.fps).toFixed(1) }}s</span>
          </div>
        </div>

        <!-- shot editor -->
        <div v-if="shot && draft" class="card">
          <div class="row" style="margin-bottom:14px">
            <h3 style="margin:0">镜头 {{ selected + 1 }} · <span class="mono">{{ shot.key }}</span></h3>
            <span v-if="dirty" class="badge" style="color:var(--warn);border-color:#5a4a1c">未保存</span>
            <span class="spacer" />
            <label class="small muted row" style="gap:6px">
              <input v-model="draft.enabled" type="checkbox" style="width:auto" @change="markDirty" /> 参与生成
            </label>
          </div>

          <div class="row wrap" style="gap:14px;align-items:flex-start">
            <div class="field" style="flex:1;min-width:200px">
              <label>镜头名称</label>
              <input v-model="draft.title" @input="markDirty" />
            </div>
            <div class="field" style="width:220px">
              <label>生成模式</label>
              <select v-model="draft.mode" @change="markDirty">
                <option v-for="m in MODES" :key="m.value" :value="m.value">{{ m.label }}</option>
              </select>
            </div>
            <div class="field" style="width:140px">
              <label>时长(秒)</label>
              <input v-model.number="draft.sec" type="number" min="0.2" max="25" step="0.1" @input="markDirty" />
              <div class="small muted" style="margin-top:5px">{{ Math.round(draft.sec * project.fps) }} 帧</div>
            </div>
          </div>
          <div class="small muted" style="margin:-8px 0 12px">{{ modeHint }}</div>

          <div class="field">
            <label>参考图(在提示词里用 &lt;Picture N&gt; 引用,N 见缩略图角标)</label>
            <div class="thumbs">
              <div v-for="(r, i) in draft.refs" :key="r + i" class="thumb">
                <img v-if="refInfo[r] && refInfo[r].url" :src="refInfo[r].url" :alt="r" />
                <div v-else class="thumb file">{{ refInfo[r] ? refInfo[r].filename : r }}</div>
                <span class="num">Picture {{ i + 1 }}</span>
                <button class="x" title="移除" @click="removeRef(i)">×</button>
              </div>
              <label class="thumb" style="display:flex;align-items:center;justify-content:center;cursor:pointer">
                <span class="small muted">+ 上传</span>
                <input type="file" accept="image/*" style="display:none" @change="addRef" />
              </label>
            </div>
            <div v-if="draft.mode === 'first_last'" class="small muted" style="margin-top:6px">
              首帧 = 第 1 张,尾帧 = 第 2 张
            </div>
          </div>

          <div class="field">
            <label>镜头提示词(可把「素材管理」里的图片直接拖进这里引用)</label>
            <textarea
              ref="promptEl"
              v-model="draft.prompt"
              :class="{ dropping: dragOver }"
              style="min-height:320px"
              @input="markDirty"
              @dragover.prevent="dragOver = true"
              @dragleave="dragOver = false"
              @drop.prevent="dropPrompt"
            />
            <div class="small muted" style="margin-top:6px">
              改完必须点下方「保存」才生效。拖入图片会自动加入本镜头参考图并插入 &lt;Picture N&gt; 引用,
              删除参考图时编号会自动重排。
            </div>
          </div>

          <div style="margin-top:14px;padding:12px 14px;border:1px solid var(--line);border-radius:10px;background:#141821">
            <div class="row" style="margin-bottom:8px">
              <b>AI 优化提示词</b>
              <span class="spacer" />
              <span class="small muted">改动只写回上面的提示词框,确认后仍需点「保存」</span>
            </div>
            <textarea
              v-model="optimizeAsk"
              style="min-height:64px"
              placeholder="告诉 AI 你想怎么改,例如:改成近景特写、光线更暖、动作别像慢回放、强调热气升起和咖啡杯细节(留空则整体润色)"
            />
            <div class="row" style="margin-top:10px">
              <button class="primary" :disabled="optimizing" @click="optimizePrompt">
                {{ optimizing ? 'AI 优化中…' : 'AI 优化' }}
              </button>
            </div>
            <div v-if="optimizeMsg" class="notice ok small" style="margin-top:10px">{{ optimizeMsg }}</div>
          </div>

          <details>
            <summary class="small muted" style="cursor:pointer">查看纯文本提示词(便于整段复制)</summary>
            <pre class="prompt-preview" style="margin-top:10px">{{ draft.prompt }}</pre>
          </details>

          <div class="row wrap" style="gap:10px;margin-top:16px">
            <button class="primary" :disabled="saving || !dirty" @click="saveDraft">
              {{ saving ? '保存中…' : dirty ? '保存' : '已保存' }}
            </button>
            <button class="ghost" :disabled="!dirty" @click="revertDraft">放弃修改</button>
            <span class="spacer" />
            <button v-if="!shotBusy" class="primary" :disabled="anyBusy" @click="renderShot">单独生成此镜头</button>
            <button v-else class="danger" @click="cancelShot">终止生成</button>
            <button class="danger" @click="deleteShot">删除镜头</button>
          </div>

          <div v-if="shotJob" style="margin-top:14px">
            <div class="row" style="margin-bottom:8px">
              <strong>单镜任务 #{{ shotJob.id }}</strong>
              <span class="badge">{{ statusText[shotJob.status] || shotJob.status }}</span>
              <span class="spacer" /><span class="small muted">{{ shotJob.message }}</span>
            </div>
            <div v-if="shotBusy" class="progress indet"><i /></div>
            <div v-else class="progress"><i :style="{ width: shotJob.progress + '%' }" /></div>
            <div v-if="shotJob.error" class="notice err small" style="margin-top:10px">{{ shotJob.error }}</div>
          </div>

          <div v-if="shot.clip_url || (shotJob && shotJob.out_url)" style="margin-top:16px">
            <label class="small muted">镜头预览(不满意可反复点「单独生成此镜头」重出)</label>
            <video :src="shot.clip_url || shotJob.out_url" controls preload="metadata" style="max-height:460px" />
          </div>
        </div>
      </div>
    </template>

    <!-- 另存为我的模板 -->
    <div v-if="showTpl" class="modal-mask" @click.self="showTpl = false">
      <div class="modal">
        <h3 style="margin-top:0">另存为我的模板</h3>
        <p class="small muted">把当前项目的全部镜头(提示词、参考图、时长、模式)保存成你自己的模板,以后一键复用。</p>
        <div class="field"><label>模板名称</label><input v-model="tplForm.title" /></div>
        <div class="field"><label>说明(可选)</label><textarea v-model="tplForm.description" style="min-height:80px" /></div>
        <div class="row">
          <button class="primary" @click="saveAsTemplate">保存模板</button>
          <span class="spacer" />
          <button class="ghost" @click="showTpl = false">取消</button>
        </div>
      </div>
    </div>

    <!-- 素材管理抽屉:人物 / 道具 / 场景 -->
    <div v-if="showAssets" class="drawer">
      <div class="head">
        <strong>素材管理</strong>
        <span class="spacer" />
        <button class="ghost" @click="showAssets = false">关闭</button>
      </div>
      <div class="body">
        <div class="small muted" style="margin-bottom:12px">
          把图片<b>拖进左侧提示词框</b>即可在提示词里引用;也可以直接点击图片插入。
        </div>

        <div class="tabs">
          <button v-for="c in CATS" :key="c.v" :class="{ on: assetTab === c.v }" @click="assetTab = c.v">
            {{ c.t }} ({{ assets.filter((a) => a.category === c.v).length }})
          </button>
        </div>

        <label style="display:block;margin-bottom:14px;cursor:pointer">
          <input type="file" accept="image/*" style="display:none" @change="uploadAsset" />
          <span class="small muted" style="display:block;text-align:center;border:1px dashed var(--line);border-radius:10px;padding:10px">
            + 上传到「{{ catName(assetTab) }}」
          </span>
        </label>

        <div class="asset-grid">
          <div v-for="a in assets.filter((x) => x.category === assetTab)" :key="a.id" class="asset">
            <img
              :src="a.url"
              draggable="true"
              :title="a.filename"
              @dragstart="dragAsset($event, a)"
              @dragend="dragEnd"
              @click="insertAsset(a)"
            />
            <template v-if="!a.is_system">
              <input v-model="a.filename" title="可改名后点保存" style="font-size:11px;padding:3px 6px" />
              <select v-model="a.category">
                <option v-for="c in CATS" :key="c.v" :value="c.v">{{ c.t }}</option>
              </select>
              <div class="ops">
                <button class="primary" @click="saveAsset(a)">保存</button>
                <button class="danger" @click="deleteAsset(a)">删除</button>
              </div>
            </template>
            <template v-else>
              <div class="name">{{ a.filename }} <span class="sys-tag">内置</span></div>
              <div class="small muted" style="font-size:11px">只读,可直接拖用</div>
            </template>
          </div>
        </div>

        <div v-if="!assets.filter((x) => x.category === assetTab).length" class="muted small" style="margin-top:10px">
          该分类下还没有素材。
        </div>
      </div>
    </div>
  </main>
</template>
