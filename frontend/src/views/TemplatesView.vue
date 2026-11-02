<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
import { store } from '../store'

const router = useRouter()
const templates = ref([])
const err = ref('')
const busy = ref(0)
const notice = ref('')

// ---- 从剧本创建:两步向导 ----
const CATS = [
  { v: 'character', k: 'characters', t: '人物' },
  { v: 'prop', k: 'props', t: '道具' },
  { v: 'scene', k: 'scenes', t: '场景' }
]
const showScript = ref(false)
const scriptStep = ref(1)
const scriptForm = reactive({ title: '', script: '', total_seconds: 20, shot_count: 5 })
const plan = ref(null)
const assignments = ref({})
const entityTab = ref('characters')
const assets = ref([])
const llm = ref(null)
const busyScript = ref(false)
const picker = ref(null)

const sysTemplates = computed(() => templates.value.filter((t) => t.is_system))
const myTemplates = computed(() => templates.value.filter((t) => !t.is_system))

function assignedCount(kind) {
  return Object.entries(assignments.value).filter(([k, v]) => k.startsWith(kind + ':') && v && v.length).length
}

function catName(v) {
  const c = CATS.find((x) => x.v === v)
  return c ? c.t : v
}

/** 某个实体已选的素材对象(行内显示缩略图用) */
function assetsOf(key) {
  return (assignments.value[key] || [])
    .map((r) => (r.startsWith('asset:') ? assets.value.find((a) => a.id === Number(r.split(':')[1])) : null))
    .filter(Boolean)
}

/* ---------- 素材选择弹窗(可多选 / 上传 / 删除) ---------- */
function openPicker(kind, name) {
  err.value = ''
  const cat = CATS.find((c) => c.k === kind) || CATS[0]
  picker.value = { kind, name, tab: cat.v, sel: [...(assignments.value[`${kind}:${name}`] || [])] }
}

function togglePick(a) {
  const ref = `asset:${a.id}`
  const i = picker.value.sel.indexOf(ref)
  if (i >= 0) picker.value.sel.splice(i, 1)
  else picker.value.sel.push(ref)
}

function confirmPicker() {
  const key = `${picker.value.kind}:${picker.value.name}`
  assignments.value = { ...assignments.value, [key]: [...picker.value.sel] }
  picker.value = null
}

async function uploadInPicker(event) {
  const file = event.target.files && event.target.files[0]
  event.target.value = ''
  if (!file) return
  try {
    const a = await api.upload(file, picker.value.tab)
    assets.value = [a, ...assets.value]
    picker.value.sel.push(`asset:${a.id}`)     // 刚上传的直接选中
  } catch (e) {
    err.value = e.message
  }
}

async function deleteInPicker(a) {
  if (!confirm(`删除素材「${a.filename}」?所有引用它的地方都会失效。`)) return
  try {
    await api.deleteAsset(a.id)
    const ref = `asset:${a.id}`
    assets.value = assets.value.filter((x) => x.id !== a.id)
    picker.value.sel = picker.value.sel.filter((r) => r !== ref)
    const next = {}
    for (const [k, v] of Object.entries(assignments.value)) next[k] = (v || []).filter((r) => r !== ref)
    assignments.value = next
  } catch (e) {
    err.value = e.message
  }
}

async function load() {
  err.value = ''
  try {
    templates.value = await api.templates()
  } catch (e) {
    err.value = e.message
  }
}

onMounted(async () => {
  await store.bootstrap()
  load()
})

async function useTemplate(t) {
  err.value = ''
  busy.value = t.id
  try {
    const project = await api.createProject({ template_id: t.id })
    router.push({ name: 'editor', params: { id: project.id } })
  } catch (e) {
    err.value = e.message
  } finally {
    busy.value = 0
  }
}

async function openScript() {
  err.value = ''
  notice.value = ''
  scriptForm.title = `我的剧本${templates.value.length + 1}`
  scriptForm.script = ''
  scriptForm.total_seconds = 20
  scriptForm.shot_count = 5
  scriptStep.value = 1
  plan.value = null
  assignments.value = {}
  entityTab.value = 'characters'
  showScript.value = true
  try {
    llm.value = await api.llmHealth()
  } catch (e) {
    llm.value = { up: false }
  }
  try {
    assets.value = await api.myAssets()
  } catch (e) {
    /* 没有素材也能继续 */
  }
}

async function analyzeScript() {
  err.value = ''
  if (!scriptForm.script.trim()) {
    err.value = '请先填写剧本内容'
    return
  }
  busyScript.value = true
  try {
    plan.value = await api.analyzeScript({ ...scriptForm, title: undefined })
    const assign = {}
    for (const c of CATS) for (const e of plan.value[c.k] || []) assign[`${c.k}:${e.name}`] = []
    assignments.value = assign
    entityTab.value = (CATS.find((c) => (plan.value[c.k] || []).length) || CATS[0]).k
    scriptStep.value = 2
  } catch (e) {
    err.value = e.message
  } finally {
    busyScript.value = false
  }
}

async function createFromScript() {
  err.value = ''
  busyScript.value = true
  try {
    const res = await api.createFromScript({
      title: scriptForm.title,
      total_seconds: scriptForm.total_seconds,
      shot_count: scriptForm.shot_count,
      mode: 'ref',
      plan: plan.value,
      assignments: assignments.value
    })
    showScript.value = false
    const used = Object.values(assignments.value).filter((v) => v && v.length).length
    notice.value =
      (res.engine === 'llm'
        ? `已用 ${res.engine_detail} 切分为 ${res.shot_count} 个镜头`
        : `模型未参与(${res.engine_detail}),已用规则切分为 ${res.shot_count} 个镜头`) +
      `,其中 ${used} 个实体指定了参考图。`
    router.push({ name: 'editor', params: { id: res.project.id } })
  } catch (e) {
    err.value = e.message
  } finally {
    busyScript.value = false
  }
}

async function removeTemplate(t) {
  if (!confirm(`删除模板「${t.title}」?`)) return
  try {
    await api.deleteTemplate(t.id)
    load()
  } catch (e) {
    err.value = e.message
  }
}

function seconds(shots) {
  return (shots.reduce((n, s) => n + s.duration_frames, 0) / 24).toFixed(1)
}
</script>

<template>
  <main class="page">
    <div class="row" style="margin-bottom:4px">
      <h1 style="margin:0">选择模板</h1>
      <span class="spacer" />
      <button class="primary" @click="openScript">从剧本创建项目</button>
    </div>
    <div class="sub">模板是一套可直接出片的分镜方案。也可以先写剧本,让模型切分镜头并指定素材。</div>

    <div v-if="notice" class="notice ok" style="margin-bottom:16px">{{ notice }}</div>
    <div v-if="err" class="notice err" style="margin-bottom:16px">{{ err }}</div>

    <h3 style="margin:18px 0 10px">系统模板</h3>
    <div class="grid">
      <div v-for="t in sysTemplates" :key="t.id" class="card">
        <img
          v-if="t.cover_url"
          :src="t.cover_url"
          alt="cover"
          style="width:100%;height:150px;object-fit:cover;border-radius:10px;margin-bottom:12px"
        />
        <div class="row">
          <h3 style="margin:0">{{ t.title }}</h3>
          <span class="spacer" />
          <span class="badge">系统</span>
        </div>
        <div class="small muted" style="margin:8px 0 10px">
          {{ t.shots.length }} 个镜头 · 约 {{ seconds(t.shots) }} 秒 · {{ t.out_width }}×{{ t.out_height }} @ {{ t.fps }}fps
        </div>
        <p class="small muted">{{ t.description }}</p>
        <div class="row" style="margin-top:12px">
          <button class="primary" :disabled="busy === t.id" @click="useTemplate(t)">
            {{ busy === t.id ? '创建中…' : '使用此模板' }}
          </button>
        </div>
      </div>
    </div>

    <h3 style="margin:26px 0 10px">我的模板</h3>
    <div class="grid">
      <div v-for="t in myTemplates" :key="t.id" class="card">
        <div class="row">
          <h3 style="margin:0">{{ t.title }}</h3>
          <span class="spacer" />
          <span class="badge">我的</span>
        </div>
        <div class="small muted" style="margin:8px 0 10px">
          {{ t.shots.length }} 个镜头 · 约 {{ seconds(t.shots) }} 秒 · {{ t.out_width }}×{{ t.out_height }} @ {{ t.fps }}fps
        </div>
        <p class="small muted">{{ t.description }}</p>
        <div class="row" style="margin-top:12px">
          <button class="primary" :disabled="busy === t.id" @click="useTemplate(t)">使用此模板</button>
          <span class="spacer" />
          <button class="danger" @click="removeTemplate(t)">删除</button>
        </div>
      </div>
    </div>
    <div v-if="!myTemplates.length" class="muted small">
      还没有自己的模板。在项目编辑器里点「另存为我的模板」就能把当前分镜方案存下来。
    </div>

    <!-- 这个弹窗故意不绑 @click.self:剧本还没提交,点旁边误关会丢失已输入内容,只能点「取消」关 -->
    <div v-if="showScript" class="modal-mask">
      <div class="modal" :style="{ width: scriptStep === 1 ? '540px' : '760px', maxHeight: '88vh', overflow: 'auto' }">
        <!-- 1/2 剧本 -->
        <template v-if="scriptStep === 1">
          <h3 style="margin-top:0">从剧本创建项目 · 1/2 填写剧本</h3>
          <p class="small muted">
            填入剧本、想要的总时长和镜头数量。下一步会先让模型把剧本里的<b>人物 / 道具 / 场景</b>列出来,
            你可以逐个指定自己上传的素材图片;不指定的就交给模型自己生成。
          </p>
          <div v-if="llm" class="small" style="margin-bottom:12px">
            <span class="badge" :class="{ email: llm.up }">
              分镜模型:{{ llm.up ? `${llm.model} 可用` : '不可用(将退回规则切分,且无法识别实体)' }}
            </span>
          </div>
          <div class="field"><label>项目名称</label><input v-model="scriptForm.title" /></div>
          <div class="field">
            <label>剧本</label>
            <textarea
              v-model="scriptForm.script"
              style="min-height:210px"
              placeholder="例:清晨,女生推开窗,阳光洒进来。她在厨房用白色马克杯冲咖啡,热气升起。她坐在桌前打开笔记本开始工作。"
            />
          </div>
          <div class="row wrap" style="gap:14px">
            <div class="field" style="width:170px">
              <label>总时长(秒)</label>
              <input v-model.number="scriptForm.total_seconds" type="number" min="2" max="600" step="1" />
            </div>
            <div class="field" style="width:170px">
              <label>分成几个镜头</label>
              <input v-model.number="scriptForm.shot_count" type="number" min="1" max="60" step="1" />
            </div>
            <div class="field" style="flex:1;min-width:180px">
              <label>时长分配</label>
              <div class="small muted" style="padding-top:8px">由模型按镜头内容决定</div>
            </div>
          </div>
          <div class="row">
            <button class="primary" :disabled="busyScript" @click="analyzeScript">
              {{ busyScript ? '模型正在分析…' : '下一步:分析人物 / 道具 / 场景' }}
            </button>
            <span class="spacer" />
            <button class="ghost" @click="showScript = false">取消</button>
          </div>
        </template>

        <!-- 2/2 指定素材 -->
        <template v-else>
          <h3 style="margin-top:0">从剧本创建项目 · 2/2 指定素材</h3>
          <p class="small muted">
            下面是模型从剧本里识别出的实体,以及它拆出的 {{ plan ? plan.shots.length : 0 }} 个镜头。
            给某个实体选一张素材图,它就会成为「出现该实体的那些镜头」的参考图;
            <b>保持「不指定」就让模型自己生成。</b>
          </p>

          <div class="tabs">
            <button
              v-for="c in CATS"
              :key="c.v"
              :class="{ on: entityTab === c.k }"
              @click="entityTab = c.k"
            >
              {{ c.t }} ({{ plan && plan[c.k] ? plan[c.k].length : 0 }})
              <span v-if="assignedCount(c.k)" style="color:var(--ok)">· 已指定 {{ assignedCount(c.k) }}</span>
            </button>
          </div>

          <div v-for="c in CATS" :key="c.k">
            <template v-if="entityTab === c.k">
              <div v-if="!(plan && plan[c.k] && plan[c.k].length)" class="muted small" style="margin:10px 0">
                剧本里没有识别到{{ c.t }}。
              </div>
              <div
                v-for="e in plan && plan[c.k] ? plan[c.k] : []"
                :key="e.name"
                class="pick-row"
              >
                <span style="width:110px"><b>{{ e.name }}</b></span>
                <span class="small muted" style="flex:1;min-width:150px">{{ e.desc }}</span>

                <div class="thumbs" style="flex:none">
                  <div
                    v-for="a in assetsOf(`${c.k}:${e.name}`)"
                    :key="a.id"
                    class="thumb"
                    style="width:46px;height:46px"
                    :title="a.filename"
                  >
                    <img :src="a.url" />
                  </div>
                  <span v-if="!(assignments[`${c.k}:${e.name}`] || []).length" class="small muted">未指定</span>
                </div>

                <button @click="openPicker(c.k, e.name)">
                  {{
                    (assignments[`${c.k}:${e.name}`] || []).length
                      ? `更换素材(${(assignments[`${c.k}:${e.name}`] || []).length})`
                      : '选择素材'
                  }}
                </button>
              </div>
            </template>
          </div>

          <div v-if="!assets.length" class="small muted" style="margin-top:10px">
            素材库里还没有图片。可以先创建项目,之后在编辑器里用「素材管理」上传并逐镜头指定。
          </div>

          <details style="margin-top:14px">
            <summary class="small muted" style="cursor:pointer">
              查看分镜({{ plan ? plan.shots.length : 0 }} 个镜头)
            </summary>
            <div
              v-for="(s, i) in plan ? plan.shots : []"
              :key="i"
              class="small"
              style="padding:6px 0;border-bottom:1px solid var(--line)"
            >
              <b>{{ i + 1 }}. {{ s.title }}</b>
              <span class="muted">
                · {{ s.seconds ? `${s.seconds.toFixed(1)}s 建议` : '按时长均分' }}
                <span v-if="(s.characters || []).length"> · 人物:{{ s.characters.join('、') }}</span>
                <span v-if="(s.props || []).length"> · 道具:{{ s.props.join('、') }}</span>
                <span v-if="(s.scenes || []).length"> · 场景:{{ s.scenes.join('、') }}</span>
              </span>
              <div class="muted">{{ s.action }}</div>
            </div>
          </details>

          <div class="row" style="margin-top:16px">
            <button class="primary" :disabled="busyScript" @click="createFromScript">
              {{ busyScript ? '正在创建…' : '切分镜头并创建项目' }}
            </button>
            <button class="ghost" :disabled="busyScript" @click="scriptStep = 1">返回修改剧本</button>
            <span class="spacer" />
            <button class="ghost" @click="showScript = false">取消</button>
          </div>
        </template>
      </div>
    </div>

    <div v-if="picker" class="modal-mask" style="z-index:70" @click.self="picker = null">
      <div class="modal" style="width:760px;max-height:88vh;overflow:auto">
        <h3 style="margin-top:0">为「{{ picker.name }}」选择素材</h3>
        <p class="small muted">
          点击图片切换选中,<b>可以选多张,顺序就是引用编号</b>
          <code>&lt;Picture 1&gt;</code>、<code>&lt;Picture 2&gt;</code>…(比如人物正面 + 背面)。
          全部不选 = 不指定,交给模型自己生成。
        </p>

        <div class="tabs" style="align-items:center">
          <button v-for="c in CATS" :key="c.v" :class="{ on: picker.tab === c.v }" @click="picker.tab = c.v">
            {{ c.t }} ({{ assets.filter((a) => a.category === c.v).length }})
          </button>
          <span class="spacer" />
          <label style="cursor:pointer">
            <input type="file" accept="image/*" style="display:none" @change="uploadInPicker" />
            <span
              class="small"
              style="display:inline-block;border:1px dashed var(--line);border-radius:8px;padding:5px 12px"
            >
              + 上传到「{{ catName(picker.tab) }}」
            </span>
          </label>
        </div>

        <div class="thumbs">
          <div
            v-for="a in assets.filter((x) => x.category === picker.tab)"
            :key="a.id"
            class="thumb"
            :class="{ picked: picker.sel.indexOf(`asset:${a.id}`) >= 0 }"
            style="width:104px;height:104px;cursor:pointer"
            :title="a.filename"
            @click="togglePick(a)"
          >
            <img :src="a.url" />
            <span v-if="picker.sel.indexOf(`asset:${a.id}`) >= 0" class="num">
              {{ picker.sel.indexOf(`asset:${a.id}`) + 1 }}
            </span>
            <button v-if="!a.is_system" class="x" title="删除素材" @click.stop="deleteInPicker(a)">×</button>
            <span
              v-if="a.is_system"
              class="num"
              style="left:auto;right:2px;background:rgba(240,180,41,.85);border-color:transparent;color:#000"
            >内置</span>
          </div>
          <div v-if="!assets.filter((x) => x.category === picker.tab).length" class="muted small">
            该分类下还没有素材,点右上角上传。
          </div>
        </div>

        <div v-if="picker.sel.length" class="small muted" style="margin-top:14px">
          已选 {{ picker.sel.length }} 张:
          <span v-for="(r, i) in picker.sel" :key="r" style="margin-right:8px">
            &#60;Picture {{ i + 1 }}&#62;
          </span>
        </div>

        <div class="row" style="margin-top:16px">
          <button class="primary" @click="confirmPicker">确定(选 {{ picker.sel.length }} 张)</button>
          <button class="ghost" :disabled="!picker.sel.length" @click="picker.sel = []">清空选择</button>
          <span class="spacer" />
          <button class="ghost" @click="picker = null">取消</button>
        </div>
      </div>
    </div>
  </main>
</template>
