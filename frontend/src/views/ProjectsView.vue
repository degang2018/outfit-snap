<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api'
import { store } from '../store'

const projects = ref([])
const err = ref('')
let timer = null

async function load() {
  err.value = ''
  try {
    projects.value = (await api.projects()).items || []
  } catch (e) {
    err.value = e.message
  }
}

onMounted(async () => {
  await store.bootstrap()
  load()
  // 列表里的状态(未生成/生成中/已完成)与成片要跟着渲染进度刷新
  timer = setInterval(load, 3000)
})

onUnmounted(() => clearInterval(timer))

async function remove(p) {
  if (!confirm(`删除项目「${p.title}」?`)) return
  await api.deleteProject(p.id)
  load()
}

const statusText = { draft: '未生成', queued: '排队中', running: '生成中', done: '已完成', failed: '失败', canceled: '已终止' }
</script>

<template>
  <main class="page">
    <h1>我的项目</h1>
    <div class="sub">每个项目都是一份可以反复修改、反复重出的分镜方案。</div>

    <div v-if="err" class="notice err" style="margin-bottom:16px">{{ err }}</div>

    <div class="grid">
      <div v-for="p in projects" :key="p.id" class="card">
        <div class="row">
          <router-link :to="{ name: 'project-detail', params: { id: p.id } }" style="color:inherit;text-decoration:none">
            <h3 style="margin:0">{{ p.title }}</h3>
          </router-link>
          <span class="spacer" />
          <span class="badge" :class="p.status">{{ statusText[p.status] || p.status }}</span>
        </div>
        <div class="small muted" style="margin:8px 0 12px">
          {{ p.total_frames }} 帧 · {{ p.duration_seconds }} 秒 · {{ p.out_width }}×{{ p.out_height }} @ {{ p.fps }}fps
        </div>
        <video v-if="p.final_url" :src="p.final_url" controls preload="metadata" style="max-height:260px;margin-bottom:12px" />
        <div v-else class="small muted" style="margin-bottom:12px">
          {{ p.status === 'running' || p.status === 'queued' ? '正在生成,完成后这里会出现成片。' : '还没有成片,打开编辑器点「开始生成」。' }}
        </div>
        <div class="row">
          <router-link :to="{ name: 'project-detail', params: { id: p.id } }"><button class="primary">查看分镜与成片</button></router-link>
          <router-link :to="{ name: 'editor', params: { id: p.id } }"><button class="ghost">编辑分镜</button></router-link>
          <a v-if="p.final_url" :href="p.final_url" download><button class="ghost">下载成片</button></a>
          <span class="spacer" />
          <button class="danger" @click="remove(p)">删除</button>
        </div>
      </div>
    </div>

    <div v-if="!projects.length && !err" class="muted">
      还没有项目,先去<a href="#/templates">选一个模板</a>吧。
    </div>
  </main>
</template>
