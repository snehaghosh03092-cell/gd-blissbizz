<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppIcon from '../shared/AppIcon.vue'
import { useRouter } from 'vue-router'
import { adminApi } from '../api/client.js'
import { session, toggleWorkspaceNav } from './store.js'

const router = useRouter()
const route = useRoute()
const mobileNav = ref(false)
const accountWorkspace = computed(() => session.owner && !session.businesses.length && ['/', '/guide', '/create'].includes(route.path))
const workspaceShell = computed(() => accountWorkspace.value || !!session.current)
watch(() => route.fullPath, () => { mobileNav.value = false })

async function logout() {
  if (session.owner) sessionStorage.removeItem(`blissbiz-guide:${session.owner.id}`)
  await adminApi.post('/auth/logout')
  adminApi.setCsrf(null)
  Object.assign(session, { owner: null, businesses: [], current: null, guideOpen: false })
  router.push('/login')
}
</script>

<template>
  <div class="admin">
    <header :class="['topbar', 'dark', { 'workspace-topbar': workspaceShell, 'nav-collapsed': session.navCollapsed }]">
      <RouterLink v-if="!workspaceShell" to="/" class="brand">BlissBiz</RouterLink>
      <nav v-if="session.owner" class="topbar-account">
        <span v-if="session.current" class="topbar-detail"><small>SHOP</small><strong>{{ session.current.name }}</strong></span>
        <span class="topbar-detail"><small>USER</small><strong>{{ session.owner.full_name }}</strong></span>
        <a v-if="!workspaceShell" href="#" @click.prevent="logout">Sign out</a>
      </nav>
    </header>
    <div v-if="accountWorkspace" :class="['starter-layout', { 'nav-collapsed': session.navCollapsed }]">
      <button class="starter-menu" type="button" :aria-expanded="mobileNav" aria-controls="starter-nav" @click="mobileNav = !mobileNav">
        <AppIcon :name="mobileNav ? 'close' : 'menu'" /> {{ mobileNav ? 'Close menu' : 'Menu' }}
      </button>
      <nav id="starter-nav" class="starter-side" :class="{ open: mobileNav, collapsed: session.navCollapsed }" aria-label="Your workspace">
        <div class="workspace-brand-row">
          <RouterLink to="/" class="workspace-brand" :title="session.navCollapsed ? 'BlissBiz' : undefined"><span>B</span><strong>BlissBiz</strong></RouterLink>
          <button class="workspace-nav-toggle" type="button" :class="{ collapsed: session.navCollapsed }" :aria-label="session.navCollapsed ? 'Expand navigation' : 'Collapse navigation'" @click="toggleWorkspaceNav"><AppIcon name="collapse" :size="17" /></button>
        </div>
        <div class="starter-identity"><span class="starter-avatar">{{ session.owner.full_name.slice(0, 1).toUpperCase() }}</span>
          <div><strong>{{ session.owner.full_name }}</strong><small>Your workspace</small></div></div>
        <p class="starter-side-label">GET STARTED</p>
        <RouterLink to="/" exact-active-class="selected" :title="session.navCollapsed ? 'Home' : undefined"><AppIcon name="home" /> <span>Home</span></RouterLink>
        <RouterLink to="/guide" active-class="selected" :title="session.navCollapsed ? 'Talk to your agent' : undefined"><AppIcon name="assistant" /> <span>Talk to your agent</span></RouterLink>
        <RouterLink to="/create" active-class="selected" :title="session.navCollapsed ? 'Create a website' : undefined"><AppIcon name="shop" /> <span>Create a website</span></RouterLink>
        <div class="starter-side-bottom">
          <div class="starter-side-note"><span class="section-icon"><AppIcon name="spark" /></span>
            <strong>Take the first step</strong><p>Explore your idea, then build your shop when you feel ready.</p></div>
          <button class="workspace-logout" type="button" :title="session.navCollapsed ? 'Sign out' : undefined" @click="logout"><AppIcon name="logout" /><span>Sign out</span></button>
        </div>
      </nav>
      <div class="starter-content"><RouterView /></div>
    </div>
    <RouterView v-else />
  </div>
</template>
