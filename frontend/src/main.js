import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import './style.css'

import { appForLocation } from './shared/hosts.js'

const application = appForLocation(window.location.hostname, window.location.pathname)
const isPlatform = application === 'platform'
const isShop = application === 'shop'

async function start() {
  const mod = isPlatform ? await import('./platform/index.js') : isShop ? await import('./shop/index.js') : await import('./admin/index.js')
  const router = createRouter({ history: createWebHistory(), routes: mod.routes, scrollBehavior: () => ({ top: 0 }) })
  if (mod.setupRouter) mod.setupRouter(router) // guards must exist before the first navigation
  const app = createApp(mod.App)
  app.config.globalProperties.window = window
  app.use(router).mount('#app')
}

start()
