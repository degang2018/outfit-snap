import { reactive, readonly } from 'vue'
import { api, clearToken, getToken, setToken } from './api'

const state = reactive({
  user: null,
  ready: false,
  assetCache: new Map(), // "asset:12" -> { url, filename }
  error: '',
  // set when an existing token no longer resolves to a user (e.g. the database was reset),
  // so the UI can explain why the project list looks empty instead of failing silently
  sessionNotice: ''
})

/**
 * Guest-first session: on first load we silently mint a guest account, so the user can
 * start a project immediately. Binding an e-mail later promotes the same account.
 */
async function bootstrap() {
  if (state.ready) return
  if (getToken()) {
    try {
      state.user = await api.me()
      state.ready = true
      return
    } catch (e) {
      clearToken()
      state.sessionNotice = '登录状态已失效(服务端数据库被重建或令牌过期),已为你重新分配游客账号,原项目无法找回。'
    }
  }
  const res = await api.guest()
  setToken(res.access_token)
  state.user = res.user
  state.ready = true
}

function applySession(res) {
  setToken(res.access_token)
  state.user = res.user
  state.assetCache.clear()
}

async function bindEmail(payload) {
  applySession(await api.bind(payload))
}

async function login(payload) {
  applySession(await api.login(payload))
}

function logout() {
  clearToken()
  state.user = null
  state.ready = false
  state.assetCache.clear()
  bootstrap()
}

/** Resolve "asset:12" / "file:H:/..." references into something renderable. */
async function resolveRef(ref) {
  if (state.assetCache.has(ref)) return state.assetCache.get(ref)
  if (ref.startsWith('file:')) {
    const info = { url: '', filename: ref.slice(5).split(/[\\/]/).pop(), local: ref.slice(5) }
    state.assetCache.set(ref, info)
    return info
  }
  try {
    const a = await api.asset(Number(ref.split(':')[1]))
    const info = { url: a.url, filename: a.filename }
    state.assetCache.set(ref, info)
    return info
  } catch (e) {
    return { url: '', filename: ref }
  }
}

export const store = {
  state: readonly(state),
  bootstrap,
  bindEmail,
  login,
  logout,
  resolveRef,
  dismissNotice() {
    state.sessionNotice = ''
  }
}
