import App from './PlatformApp.vue'
import { adminApi } from '../api/client.js'
import { reactive } from 'vue'

export { App }
export const me = reactive({ owner: null, checked: false })

export const routes = [
  { path: '/login', component: () => import('./PlatformLogin.vue'), meta: { public: true } },
  { path: '/', component: () => import('./PlatformHome.vue') },
  { path: '/audit', component: () => import('./PlatformAudit.vue') },
  { path: '/:rest(.*)*', redirect: '/' },
]

export function setupRouter(router) {
  router.beforeEach(async (to) => {
    if (!me.checked) {
      me.owner = await adminApi.get('/auth/me').then((r) => r.owner).catch(() => null)
      me.checked = true
    }
    if (!to.meta.public && me.owner?.platform_role !== 'admin') return { path: '/login', query: { next: to.fullPath } }
    return true
  })
}
