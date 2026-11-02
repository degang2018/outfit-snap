<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { store } from './store'

const state = store.state
const showBind = ref(false)
const userOpen = ref(false)
const form = reactive({ email: '', password: '', display_name: '' })
const msg = ref('')
const busy = ref(false)

const avatarText = computed(() => {
  const u = state.user
  if (!u) return ''
  const s = (u.is_guest ? u.display_name || '游' : u.email || 'U').trim()
  return s.slice(0, 1).toUpperCase()
})

onMounted(() => {
  store.bootstrap()
  // 点页面其它地方收起用户下拉(几处 @click.stop 保证点菜单本身不会关)
  document.addEventListener('click', () => {
    userOpen.value = false
  })
})

function openBind() {
  userOpen.value = false
  form.email = ''
  form.password = ''
  showBind.value = true
}

function doLogout() {
  userOpen.value = false
  if (!confirm('退出登录?退出后会以新的游客身份进入;原账号可以随时用邮箱重新登录。')) return
  store.logout()
}

async function submitBind() {
  msg.value = ''
  busy.value = true
  try {
    await store.bindEmail({ ...form })
    showBind.value = false
    form.password = ''
    msg.value = ''
  } catch (e) {
    msg.value = e.message
  } finally {
    busy.value = false
  }
}

async function submitLogin() {
  msg.value = ''
  busy.value = true
  try {
    await store.login({ email: form.email, password: form.password })
    showBind.value = false
    form.password = ''
  } catch (e) {
    msg.value = e.message
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <header class="app-header">
    <router-link to="/templates" class="logo" style="color:inherit;text-decoration:none" title="回到模板首页">衣秒成片</router-link>
    <nav>
      <router-link to="/templates">模板</router-link>
      <router-link to="/projects">我的项目</router-link>
    </nav>
    <span class="spacer" />
    <div v-if="state.user" class="user-menu">
      <button class="user-chip" :class="{ on: userOpen }" @click.stop="userOpen = !userOpen">
        <span class="avatar" :class="{ guest: state.user.is_guest }">{{ avatarText }}</span>
        <span>{{ state.user.is_guest ? state.user.display_name + '(游客)' : state.user.email }}</span>
        <span class="caret">▼</span>
      </button>
      <div v-if="userOpen" class="menu" @click.stop>
        <template v-if="state.user.is_guest">
          <div class="menu-info">当前是游客身份,项目和素材绑定在本机。绑定邮箱后可以长期保存、换设备也能登录。</div>
          <button class="menu-item" @click="openBind">绑定邮箱 / 登录已有账号</button>
        </template>
        <template v-else>
          <div class="menu-info">已登录:{{ state.user.email }}</div>
          <button class="menu-item danger" @click="doLogout">退出登录</button>
        </template>
      </div>
    </div>
  </header>

  <div v-if="state.sessionNotice" class="page" style="padding-bottom:0">
    <div class="notice err row" style="margin-bottom:0">
      <span style="flex:1">{{ state.sessionNotice }}</span>
      <button class="ghost" @click="store.dismissNotice()">知道了</button>
    </div>
  </div>

  <router-view />

  <div v-if="showBind" class="modal-mask" @click.self="showBind = false">
    <div class="modal">
      <h3 style="margin-top:0">绑定邮箱</h3>
      <p class="small muted">绑定后账号可长期保存,当前的项目和素材会自动保留。</p>
      <div class="field">
        <label>邮箱</label>
        <input v-model="form.email" placeholder="you@example.com" />
      </div>
      <div class="field">
        <label>密码(至少 6 位)</label>
        <input v-model="form.password" type="password" placeholder="******" />
      </div>
      <div class="field">
        <label>昵称(可选)</label>
        <input v-model="form.display_name" placeholder="怎么称呼你" />
      </div>
      <div v-if="msg" class="notice err small" style="margin-bottom:12px">{{ msg }}</div>
      <div class="row">
        <button class="primary" :disabled="busy" @click="submitBind">绑定</button>
        <button class="ghost" :disabled="busy" @click="submitLogin">已有账号,直接登录</button>
        <span class="spacer" />
        <button class="ghost" @click="showBind = false">取消</button>
      </div>
    </div>
  </div>
</template>
