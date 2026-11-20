<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { adminApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import Pager from '../shared/Pager.vue'
import StatusBadge from '../shared/StatusBadge.vue'
import { money } from '../shared/format.js'
import { base, can } from './store.js'

const route = useRoute()
const router = useRouter()
const b = base(route.params.bid)
const data = ref(null)
const categories = ref([])
const error = ref(null)
const creating = ref(false)
const q = ref(route.query.q || '')
const form = ref({ name: '', category_id: '', tax_class: 'GST_5', hsn_sac: '', stock_unit: 'PCS', brand: '' })
const newCategory = ref('')

adminApi.get(b + '/products', { status: route.query.status, q: route.query.q, page: route.query.page || 1 })
  .then((r) => (data.value = r)).catch((e) => (error.value = e))
const loadCategories = () => adminApi.get(b + '/categories').then((r) => (categories.value = r.items))
loadCategories()

async function addCategory() {
  error.value = null
  try {
    const c = await adminApi.post(b + '/categories', { name: newCategory.value })
    await loadCategories()
    form.value.category_id = c.id
    newCategory.value = ''
  } catch (e) {
    error.value = e
  }
}

async function create() {
  error.value = null
  try {
    const body = { ...form.value, category_id: form.value.category_id || null, hsn_sac: form.value.hsn_sac || null, brand: form.value.brand || null }
    const p = await adminApi.post(b + '/products', body)
    router.push(`/b/${route.params.bid}/products/${p.id}`)
  } catch (e) {
    error.value = e
  }
}
</script>

<template>
  <main class="workspace-page products-page">
  <header class="workspace-page-head">
    <div><p class="eyebrow">CATALOG</p><h1>Products</h1><p>Manage the items customers see, their prices, variants and publishing status.</p></div>
    <button v-if="can('catalog.write')" @click="creating = !creating">{{ creating ? 'Close form' : 'New product' }}</button>
  </header>
  <form v-if="creating" class="card stack" @submit.prevent="create">
    <div class="grid3">
      <label>Name <input v-model="form.name" required maxlength="200" /></label>
      <label>Brand <input v-model="form.brand" maxlength="100" /></label>
      <label>Category <select v-model="form.category_id"><option value="">None</option><option v-for="c in categories" :key="c.id" :value="c.id">{{ c.name }}</option></select></label>
      <label>GST class <select v-model="form.tax_class"><option v-for="t in ['GST_0', 'GST_5', 'GST_18', 'GST_40', 'EXEMPT', 'NONE']" :key="t">{{ t }}</option></select></label>
      <label>HSN/SAC <input v-model="form.hsn_sac" pattern="[0-9]{4}([0-9]{2}){0,2}" placeholder="e.g. 6109" /></label>
      <label>Sold in <select v-model="form.stock_unit"><option v-for="u in ['PCS', 'PACK', 'PAIR', 'BOX', 'DOZEN', 'KG', 'G', 'L', 'ML']" :key="u">{{ u }}</option></select></label>
    </div>
    <div class="row"><input v-model="newCategory" placeholder="New category name" maxlength="100" aria-label="New category" />
      <button type="button" class="ghost" :disabled="!newCategory" @click="addCategory">Add category</button></div>
    <button>Create draft</button>
  </form>
  <form class="filters admin-filterbar" @submit.prevent="router.push({ query: { ...route.query, q: q || undefined, page: undefined } })">
    <select :value="route.query.status || ''" aria-label="Status" @change="(e) => router.push({ query: { ...route.query, status: e.target.value || undefined, page: undefined } })">
      <option value="">All</option><option value="PUBLISHED">Published</option><option value="DRAFT">Draft</option><option value="ARCHIVED">Archived</option>
    </select>
    <input v-model="q" placeholder="Search name" aria-label="Search" /><button>Search</button>
  </form>
  <ErrorBox :error="error" />
  <div v-if="data" class="table-card"><table>
    <thead><tr><th>Product</th><th>Category</th><th>Status</th><th class="num">Variants</th><th class="num">Price</th></tr></thead>
    <tbody><tr v-for="p in data.items" :key="p.id">
      <td><RouterLink :to="`/b/${route.params.bid}/products/${p.id}`">{{ p.name }}</RouterLink></td><td>{{ p.category || '—' }}</td>
      <td><StatusBadge :status="p.status" /></td><td class="num">{{ p.variants }}</td>
      <td class="num">{{ p.price_min === p.price_max ? money(p.price_min) : `${money(p.price_min)} – ${money(p.price_max)}` }}</td></tr></tbody>
  </table></div>
  <Pager v-if="data" :page="data.page" :page-size="data.page_size" :total="data.total" @go="(n) => router.push({ query: { ...route.query, page: n } })" />
  </main>
</template>
