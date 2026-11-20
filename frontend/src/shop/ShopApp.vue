<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { shopApi } from '../api/client.js'
import Assistant from '../shared/Assistant.vue'
import { storefrontDesign } from '../shared/storefrontTemplates.js'
import { cartCount, loadCart, loadShop, state } from './store.js'

const router = useRouter()
const q = ref('')
const chat = ref(false)
const design = computed(() => storefrontDesign(state.shop?.template_key, state.shop?.theme))
const examples = ['black dresses under 1500', 'what is the return policy?', 'where is my order?']
onMounted(loadShop)

function search() {
  router.push({ path: '/', query: q.value ? { q: q.value } : {} })
}

async function logout() {
  await shopApi.post('/auth/logout')
  state.customer = null
  state.owner = null
  state.ownerBusiness = null
  shopApi.setCsrf(null)
  await loadCart()
  router.push('/')
}
</script>

<template>
  <div v-if="state.shopError" class="page center">
    <h1>Shop not found</h1>
    <p class="muted">{{ state.shopError.status === 404 ? 'There is no open shop at this address.' : state.shopError.message }}</p>
  </div>
  <!-- pages wait until we know the shop AND who is signed in (deep links to /orders, /checkout) -->
  <div v-else-if="state.shop && state.ready" :class="['shop', ...design.classes]" :style="design.colors">
    <header class="topbar">
      <div class="shop-nav-shell">
      <RouterLink to="/" class="brand">{{ state.shop.theme?.logo_text || state.shop.business.name }}</RouterLink>
      <form class="search" role="search" @submit.prevent="search">
        <input v-model="q" type="search" placeholder="Search, e.g. black tee" aria-label="Search products" />
        <button>Search</button>
      </form>
      <nav class="row">
        <RouterLink to="/cart">Cart ({{ cartCount() }})</RouterLink>
        <template v-if="state.owner && state.ownerBusiness">
          <span>{{ state.owner.full_name }}</span>
          <a :href="`/b/${state.ownerBusiness.id}/overview`">Manage shop</a>
          <a href="#" @click.prevent="logout">Sign out</a>
        </template>
        <template v-else-if="state.customer">
          <RouterLink to="/orders">My orders</RouterLink>
          <a href="#" @click.prevent="logout">Sign out</a>
        </template>
        <RouterLink v-else to="/login">Sign in</RouterLink>
      </nav>
      </div>
    </header>
    <main class="page"><RouterView /></main>
    <button class="fab" :aria-expanded="chat" @click="chat = !chat">{{ chat ? 'Close' : 'Ask us' }}</button>
    <div v-if="chat" class="fab-panel">
      <Assistant :api="shopApi" path="/agent" :examples="examples" :title="`Ask ${state.shop.business.name}`" />
    </div>
    <footer class="footer">
      <span>{{ state.shop.business.name }} · {{ state.shop.business.city }}</span>
      <RouterLink to="/policies">Shipping, returns &amp; policies</RouterLink>
      <span v-if="state.shop.business.contact_phone">Call {{ state.shop.business.contact_phone }}</span>
    </footer>
  </div>
  <div v-else class="page center"><p class="muted">Loading…</p></div>
</template>
