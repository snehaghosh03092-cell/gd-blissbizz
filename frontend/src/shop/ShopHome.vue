<script setup>
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { shopApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import Pager from '../shared/Pager.vue'
import StatusBadge from '../shared/StatusBadge.vue'
import StorefrontHero from '../shared/StorefrontHero.vue'
import { money } from '../shared/format.js'
import { state } from './store.js'

const route = useRoute()
const router = useRouter()
const categories = ref([])
const result = ref(null)
const error = ref(null)
const loading = ref(false)
const f = ref({})

shopApi.get('/categories').then((r) => (categories.value = r.items)).catch(() => {})

function fromQuery() {
  const q = route.query
  f.value = { q: q.q || '', category: q.category || '', color: q.color || '', size: q.size || '',
    min_price: q.min_price || '', max_price: q.max_price || '', in_stock: q.in_stock === 'true',
    sort: q.sort || 'relevance', page: Number(q.page || 1) }
}

async function load() {
  fromQuery()
  loading.value = true
  error.value = null
  try {
    result.value = await shopApi.get('/products', { ...f.value, in_stock: f.value.in_stock || '', page_size: 24 })
  } catch (e) {
    error.value = e
  } finally {
    loading.value = false
  }
}

function apply(extra = {}) {
  const next = { ...f.value, page: 1, ...extra }
  const query = Object.fromEntries(Object.entries(next).filter(([k, v]) => v !== '' && v !== false && !(k === 'page' && v === 1) && !(k === 'sort' && v === 'relevance')))
  router.push({ path: '/', query })
}

watch(() => route.query, load, { immediate: true })
</script>

<template>
  <StorefrontHero v-if="!route.query.q && state.shop.config?.hero" :title="state.shop.config.hero.title"
    :subtitle="state.shop.config.hero.subtitle" />
  <section class="catalog-section">
  <header class="catalog-heading">
    <div><p class="catalog-kicker">SHOP THE COLLECTION</p><h2>Find something you’ll love</h2></div>
    <p v-if="result" class="catalog-count">{{ result.total }} product{{ result.total === 1 ? '' : 's' }}</p>
  </header>
  <div class="chips" v-if="categories.length">
    <button :class="['chip', !f.category && 'on']" @click="apply({ category: '' })">All</button>
    <button v-for="c in categories" :key="c.slug" :class="['chip', f.category === c.slug && 'on']"
            @click="apply({ category: c.slug })">{{ c.name }} <small>{{ c.products }}</small></button>
  </div>
  <form class="filters storefront-filters" @submit.prevent="apply()">
    <label><span>Search</span><input v-model="f.q" placeholder="Product or brand" /></label>
    <label><span>Colour</span><input v-model="f.color" placeholder="Any colour" /></label>
    <label><span>Size</span><input v-model="f.size" placeholder="Any size" /></label>
    <label><span>Minimum price</span><input v-model="f.min_price" type="number" min="0" placeholder="₹0" /></label>
    <label><span>Maximum price</span><input v-model="f.max_price" type="number" min="0" placeholder="No limit" /></label>
    <label><span>Sort by</span><select v-model="f.sort">
      <option value="relevance">Relevance</option><option value="price_asc">Price: low to high</option>
      <option value="price_desc">Price: high to low</option><option value="newest">Newest</option><option value="name">Name</option>
    </select></label>
    <label class="stock-toggle"><input v-model="f.in_stock" type="checkbox" /><span>In stock only</span></label>
    <button>Show results</button>
  </form>
  <ErrorBox :error="error" />
  <p v-if="loading" class="muted">Loading…</p>
  <template v-else-if="result">
    <p v-if="f.q" class="muted catalog-query">Results for “{{ f.q }}”</p>
    <p v-if="!result.items.length" class="empty">Nothing matches. Try fewer filters.</p>
    <div class="grid shop-catalog">
      <RouterLink v-for="p in result.items" :key="p.id" :to="`/p/${p.slug}`" class="card product">
        <div class="thumb">{{ p.name.slice(0, 1) }}</div>
        <h3>{{ p.name }}</h3>
        <p class="muted small">{{ p.brand }} · {{ p.category }}</p>
        <p class="price">{{ p.price_min === p.price_max ? money(p.price_min) : `${money(p.price_min)} – ${money(p.price_max)}` }}</p>
        <StatusBadge :status="p.availability" />
        <small v-if="p.available_quantity" class="muted"> only {{ p.available_quantity }} left</small>
      </RouterLink>
    </div>
    <Pager :page="result.page" :page-size="result.page_size" :total="result.total" @go="(n) => apply({ page: n })" />
  </template>
  </section>
</template>
