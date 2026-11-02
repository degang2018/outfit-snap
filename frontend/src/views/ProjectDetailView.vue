<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
import { store } from '../store'

const props = defineProps({ id: { type: [String, Number], required: true } })
const router = useRouter()

const project = ref(null)
const err = ref('')
let timer = null

async function load() {
  try {
    project.value = await api.project(props.id)
  } catch (e) {
    err.value = e.message
  }
}

onMounted(async () => {
  await store.bootstrap()
  await load()
  // 生成中时,各分镜片段与合成都跟着刷新
  timer = setInterval(load, 3000)
})

onUnmounted(() => clearInterval(timer))

const statusText = { draft: '未生成', queued: '排队中', running: '生成中', done: '已完成', failed: '失败', canceled: '已终止' }
const doneShots = computed(() => (project.value ? project.value.shots.filter((s) => s.clip_url).length : 0))

/** 该项目当前在跑的任务(整片渲染) */
const activeJob = computed(() => {
  const j = project.value && project.value.latest_job
  return j && ['queued', 'running'].includes(j.status) ? j : null
})

/** 这个分镜是不是正被生成:整片任务靠 current_shot 认领 */
function jobForShot(s) {
  const j = activeJob.value
  return j && j.current_shot === s.key ? j : null
}

/** 终止正在生成这个分镜的任务 */
async function cancelShotRender(s) {
  const j = jobForShot(s)
  if (!j) return
  if (!confirm(`镜头 ${s.idx} 正在整片渲染中。终止会停掉整片渲染(已生成好的分镜片段会保留)。确定终止吗?`)) return
  err.value = ''
  try {
    await api.cancelJob(j.id)
    await load()
  } catch (e) {
    err.value = e.message
  }
}

/** 分镜的真实状态:有片段才算已生成 */
function shotLabel(s) {
  if (s.clip_url) return '已生成'
  if (s.status === 'running' || (s.status === 'queued' && project.value.status === 'running')) return '生成中'
  if (s.status === 'failed') return '失败'
  return '未生成'
}

function shotBadge(s) {
  const label = shotLabel(s)
  if (label === '已生成') return 'done'
  if (label === '生成中') return 'running'
  if (label === '失败') return 'failed'
  return 'draft'
}

async function deleteShotClip(s) {
  if (!s.clip_url) return
  if (!confirm(`删除分镜 ${s.idx}(${s.title || s.key})已生成的视频?分镜会回到「未生成」。`)) return
  err.value = ''
  try {
    const updated = await api.deleteShotClip(project.value.id, s.id)
    Object.assign(project.value.shots.find((x) => x.id === s.id), updated)
  } catch (e) {
    err.value = e.message
  }
}

async function deleteFilm() {
  if (!project.value || !project.value.final_url) return
  if (!confirm('删除已经合成的整片视频?(各分镜自己的片段会保留)')) return
  err.value = ''
  try {
    project.value = await api.deleteFilm(project.value.id)
  } catch (e) {
    err.value = e.message
  }
}
</script>

<template>
  <main class="page">
    <div v-if="err" class="notice err" style="margin-bottom:14px">{{ err }}</div>
    <div v-if="!project" class="muted">加载中…</div>

    <template v-else>
      <div class="row wrap" style="gap:10px;margin-bottom:6px">
        <button class="ghost" @click="router.push({ name: 'projects' })">← 我的项目</button>
        <h2 style="margin:0">{{ project.title }}</h2>
        <span class="badge" :class="project.status">{{ statusText[project.status] || project.status }}</span>
        <span class="spacer" />
        <router-link :to="{ name: 'editor', params: { id: project.id } }"><button>编辑分镜</button></router-link>
      </div>
      <div class="sub">
        {{ project.shots.length }} 个分镜 · {{ doneShots }} 个已生成 · {{ project.total_frames }} 帧 ·
        {{ project.duration_seconds }} 秒 · {{ project.out_width }}×{{ project.out_height }} @ {{ project.fps }}fps
      </div>

      <div class="card" style="margin-bottom:16px">
        <div class="row" style="margin-bottom:10px">
          <strong>合成视频(整片)</strong>
          <span class="spacer" />
          <a v-if="project.final_url" :href="project.final_url" download><button class="ghost">下载</button></a>
          <button v-if="project.final_url" class="danger" @click="deleteFilm">删除</button>
        </div>
        <video v-if="project.final_url" :src="project.final_url" controls preload="metadata" style="max-height:440px" />
        <div v-else class="small muted">还没有合成成片。各分镜生成好后,去编辑器点「开始生成」合成整片。</div>
      </div>

      <div class="card">
        <div class="row" style="margin-bottom:12px">
          <strong>分镜清单</strong>
          <span class="spacer" />
          <span class="small muted">已生成 {{ doneShots }} / {{ project.shots.length }}</span>
        </div>
        <div v-for="s in project.shots" :key="s.id" class="shot-row">
          <div class="row wrap" style="gap:8px;margin-bottom:8px">
            <span class="small muted" style="width:24px">{{ s.idx }}</span>
            <strong class="small">{{ s.title || s.key }}</strong>
            <span class="badge" :class="shotBadge(s)">{{ shotLabel(s) }}</span>
            <span class="small muted">{{ (s.duration_frames / project.fps).toFixed(1) }}s · {{ s.duration_frames }} 帧</span>
            <span class="spacer" />
            <button v-if="jobForShot(s)" class="danger" @click="cancelShotRender(s)">取消生成</button>
            <a v-if="s.clip_url" :href="s.clip_url" download><button class="ghost">下载</button></a>
            <button v-if="s.clip_url" class="danger" @click="deleteShotClip(s)">删除</button>
          </div>
          <video v-if="s.clip_url" :src="s.clip_url" controls preload="metadata" style="max-height:300px" />
          <div v-else class="small muted">{{ jobForShot(s) ? '正在生成中…' : '未生成' }}</div>
        </div>
      </div>
    </template>
  </main>
</template>

<style scoped>
.shot-row {
  padding: 14px 0;
  border-top: 1px solid var(--line);
}
.shot-row:first-of-type {
  border-top: none;
  padding-top: 0;
}
</style>
