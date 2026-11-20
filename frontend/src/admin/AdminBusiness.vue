<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { adminApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import AppIcon from '../shared/AppIcon.vue'
import Assistant from '../shared/Assistant.vue'
import { storefrontUrl } from '../shared/hosts.js'
import { base, can, enterBusiness, session, toggleWorkspaceNav } from './store.js'

const route = useRoute()
const router = useRouter()
const error = ref(null)
const mobileNav = ref(false)
const startupGuideExamples = [
  'I have ₹1 lakh but no business idea. What can I start?',
  'What papers do I need to start a small business?',
  'How can I test demand before spending money?',
  'How should I price products to make a profit?',
]
const liveGuideExamples = [
  'Compare my stock cover, incoming orders and demand. What needs attention?',
  'Which products tie up cash, and what could I do about it?',
  'What should I reorder first, and why?',
  'Collect my invoices for GST in Excel',
  'Prepare my GST and shop registration documents',
  'Export my inventory to Excel',
]
watch([() => route.params.bid, () => route.path], async ([bid]) => {
  if (!bid) return
  error.value = null
  try {
    await enterBusiness(bid)
    const child = String(route.path.split(`/b/${bid}/`)[1] || '').split('/')[0]
    if (session.current.status === 'ONBOARDING') {
      if (!['', 'setup', 'assistant'].includes(child)) {
        await router.replace(`/b/${bid}`)
      }
    } else if (!child) {
      await router.replace(`/b/${bid}/overview`)
    }
  } catch (e) {
    error.value = e
    session.current = null
  }
}, { immediate: true })

const onboarding = computed(() => session.current?.status === 'ONBOARDING')
const guideExamples = computed(() => onboarding.value ? startupGuideExamples : liveGuideExamples)
const liveWalkthrough = computed(() => onboarding.value ? null : ({
  text: '### Your business, in focus\n\nAsk me about stock, sales, recommendations or your next business decision. I can combine your shop data into a clear explanation and a practical next step.\n\nTry: **Which products could run out before the next delivery, and what should I prioritise?**',
}))
const isLaunch = computed(() => route.path.replace(/\/$/, '') === `/b/${route.params.bid}`)
const openGroup = ref('build')
const navGroups = [
  { id: 'overview', label: 'Overview', icon: 'home', direct: ['overview', 'Overview', 'business.read'] },
  { id: 'build', label: 'Build & plan', icon: 'setup', items: [
    ['assistant', 'Talk to your agent', 'agent.use'], ['setup', 'Create a website', 'business.read'],
  ] },

  { id: 'sales', label: 'Sales', icon: 'orders', items: [
    ['orders', 'Orders', 'sales.read'], ['returns', 'Returns', 'sales.read'], ['invoices', 'Invoices', 'invoices.read'],
  ] },
  { id: 'catalog', label: 'Catalog', icon: 'products', items: [
    ['products', 'Products', 'catalog.read'], ['inventory', 'Inventory', 'inventory.read'],
  ] },
  { id: 'purchasing', label: 'Purchasing', icon: 'suppliers', items: [
    ['suppliers', 'Suppliers', 'purchasing.read'], ['purchase-orders', 'Purchase orders', 'purchasing.read'],
    ['supplier-invoices', 'Supplier invoices', 'purchasing.read'], ['payments', 'Supplier payments', 'purchasing.read'],
  ] },
]
const visibleGroups = computed(() => navGroups.flatMap((group) => {
  if (onboarding.value) {
    if (group.id !== 'build') return []
    const items = group.items.filter(([path, , permission]) => ['assistant', 'setup'].includes(path) && can(permission))
    return items.length ? [{ ...group, label: 'Get started', items }] : []
  }
  if (group.id === 'build') {
    const items = group.items
      .filter(([path, , permission]) => path === 'setup' && can(permission))
      .map(([path, , permission]) => [path, 'Update website', permission])
    return items.length ? [{ ...group, label: 'Website', items }] : []
  }
  if (group.direct) return can(group.direct[2]) ? [group] : []
  const items = group.items.filter(([, , permission]) => can(permission))
  return items.length ? [{ ...group, items }] : []
}))
const activeChild = computed(() => String(route.path.split(`/b/${route.params.bid}/`)[1] || '').split('/')[0])

watch([activeChild, visibleGroups], ([child, groups]) => {
  const active = groups.find((group) => group.items?.some(([path]) => path === child))
  if (active) openGroup.value = active.id
  else if (groups.some((group) => group.direct?.[0] === child)) openGroup.value = ''
}, { immediate: true })

function groupActive(group) {
  return group.direct?.[0] === activeChild.value || group.items?.some(([path]) => path === activeChild.value)
}

function toggleGroup(group) {
  openGroup.value = openGroup.value === group.id ? '' : group.id
  if (session.navCollapsed) toggleWorkspaceNav()
}

function handleGuideNavigation(ui) {
  if (ui.navigate === 'setup') {
    session.guideOpen = false
  }
}

async function logout() {
  if (session.owner) sessionStorage.removeItem(`blissbiz-guide:${session.owner.id}`)
  await adminApi.post('/auth/logout')
  adminApi.setCsrf(null)
  Object.assign(session, { owner: null, businesses: [], current: null, guideOpen: false })
  router.push('/login')
}
</script>

<template>
  <main v-if="error" class="page"><ErrorBox :error="error" /></main>
  <div v-if="session.current && session.current.id === route.params.bid" :class="['admin-layout', { 'launch-layout': isLaunch, 'nav-collapsed': session.navCollapsed }]">
    <button class="mobile-nav-button" type="button" :aria-expanded="mobileNav" @click="mobileNav = !mobileNav">
      <AppIcon :name="mobileNav ? 'close' : 'menu'" /> Menu
    </button>
    <nav :class="['admin-side', mobileNav && 'open', session.navCollapsed && 'collapsed']" aria-label="Business navigation">
      <div class="workspace-brand-row">
        <RouterLink to="/" class="workspace-brand" :title="session.navCollapsed ? 'BlissBiz' : undefined"><span>B</span><strong>BlissBiz</strong></RouterLink>
        <button class="workspace-nav-toggle" type="button" :class="{ collapsed: session.navCollapsed }" :aria-label="session.navCollapsed ? 'Expand navigation' : 'Collapse navigation'" @click="toggleWorkspaceNav"><AppIcon name="collapse" :size="17" /></button>
      </div>
      <div class="business-mark">
        <span class="business-avatar">{{ session.current.name.slice(0, 1).toUpperCase() }}</span>
        <span><strong>{{ session.current.name }}</strong><small>{{ onboarding ? 'Let’s build your shop' : 'Shop workspace' }}</small></span>
      </div>
      <p class="side-label">{{ onboarding ? 'GET STARTED' : 'NAVIGATION' }}</p>
      <div class="admin-nav-tree">
        <button v-if="!onboarding && can('agent.use')" type="button" class="nav-direct guide-nav-link" :title="session.navCollapsed ? 'Business Guide' : undefined" @click="session.guideOpen = true; mobileNav = false"><span class="nav-group-icon"><AppIcon name="assistant" /></span><span>Business Guide</span></button>
        <template v-for="group in visibleGroups" :key="group.id">
          <RouterLink v-if="group.direct" class="nav-direct" :to="`/b/${route.params.bid}/${group.direct[0]}`"
            :title="session.navCollapsed ? group.label : undefined" @click="openGroup = ''; mobileNav = false">
            <span class="nav-group-icon"><AppIcon :name="group.icon" /></span><span>{{ group.label }}</span>
          </RouterLink>
          <section v-else :class="['nav-group', { open: openGroup === group.id, active: groupActive(group) }]">
            <button type="button" class="nav-group-trigger" :title="session.navCollapsed ? group.label : undefined"
              :aria-expanded="openGroup === group.id" @click="toggleGroup(group)">
              <span class="nav-group-icon"><AppIcon :name="group.icon" /></span>
              <span class="nav-group-label">{{ group.label }}</span>
              <AppIcon class="nav-group-chevron" name="chevronDown" :size="16" />
            </button>
            <div v-show="openGroup === group.id && (!session.navCollapsed || mobileNav)" class="nav-submenu">
              <RouterLink v-for="[path, label] in group.items" :key="path" :to="`/b/${route.params.bid}/${path}`"
                @click="mobileNav = false">{{ label }}</RouterLink>
            </div>
          </section>
        </template>
      </div>
      <div class="admin-side-bottom">
        <a v-if="!onboarding && session.current.slug" class="view-shop" :href="storefrontUrl(session.current.slug)" target="_blank" rel="noopener" :title="session.navCollapsed ? 'View shop' : undefined">
          <AppIcon name="shop" /><span>View shop</span><AppIcon class="external-icon" name="external" :size="15" />
        </a>
        <button class="workspace-logout" type="button" :title="session.navCollapsed ? 'Sign out' : undefined" @click="logout"><AppIcon name="logout" /><span>Sign out</span></button>
      </div>
    </nav>
    <section class="admin-content"><RouterView :key="route.fullPath" /></section>
    <template v-if="can('agent.use')">
      <button v-if="!isLaunch" class="guide-fab" type="button" aria-label="Open Business Guide" :aria-expanded="session.guideOpen" aria-controls="business-guide-drawer" @click="session.guideOpen = true">
        <span><AppIcon name="spark" :size="23" /></span><strong>Business guide</strong>
      </button>
      <div v-if="session.guideOpen" class="drawer-backdrop" @click="session.guideOpen = false"></div>
      <aside v-if="session.guideOpen" id="business-guide-drawer" class="guide-drawer open" aria-label="Business guide" @keydown.esc="session.guideOpen = false">
        <header>
          <span class="guide-orb"><AppIcon name="spark" :size="23" /></span>
          <div><p class="eyebrow">BUSINESS GUIDE</p><h2>{{ onboarding ? 'Plan before you build' : 'Your business, in focus' }}</h2></div>
          <button type="button" class="drawer-close" aria-label="Close business guide" @click="session.guideOpen = false"><AppIcon name="close" /></button>
        </header>
        <p class="drawer-intro">{{ onboarding ? 'Ask about business ideas, registrations, GST, pricing and starting small.' : 'One place for business advice, inventory analysis and answers grounded in your shop data.' }}</p>
        <Assistant :key="route.params.bid" :api="adminApi" :path="`${base(route.params.bid)}/business-guide`" :link-base="`/b/${route.params.bid}/`"
          :examples="guideExamples" :welcome="liveWalkthrough" :title="onboarding ? 'What are you thinking of building?' : 'Ask your Business Guide'" :prompt="session.guidePrompt"
          :storage-key="`blissbiz-guide-v2:${session.owner?.id}:${route.params.bid}`" @navigate="handleGuideNavigation" @prompt-consumed="session.guidePrompt = null" />
        <p class="guide-note"><AppIcon name="check" :size="16" /> General guidance only. Confirm legal and tax steps on the linked official portals.</p>
      </aside>
    </template>
  </div>
</template>
