<script setup>
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { adminApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import StatusBadge from '../shared/StatusBadge.vue'
import { money, options } from '../shared/format.js'
import { base, can } from './store.js'

const route = useRoute()
const b = base(route.params.bid)
const pid = route.params.pid
const p = ref(null)
const edit = ref({})
const categories = ref([])
const warehouses = ref([])
const error = ref(null)
const note = ref('')
const busy = ref(false)
const variant = ref({ sku: '', options: '', selling_price: '', mrp: '', cost_price: '', reorder_threshold: '0' })
const opening = ref({ variant_id: '', warehouse_id: '', quantity: '', unit_cost: '' })
const writer = computed(() => can('catalog.write'))

function show(r) {
  p.value = r
  edit.value = { name: r.name, brand: r.brand || '', description: r.description || '', category_id: r.category_id || '',
    tax_class: r.tax_class, hsn_sac: r.hsn_sac || '', stock_unit: r.stock_unit }
}
async function load() {
  show(await adminApi.get(`${b}/products/${pid}`))
}
load().catch((e) => (error.value = e))
adminApi.get(b + '/categories').then((r) => (categories.value = r.items))
adminApi.get(b + '/warehouses').then((r) => {
  warehouses.value = r.items
  opening.value.warehouse_id = r.items.find((w) => w.is_default)?.id || r.items[0]?.id || ''
}).catch(() => {})

async function run(fn, done = '') {
  busy.value = true
  error.value = null
  note.value = ''
  try {
    const r = await fn()
    if (r?.variants) show(r)
    await load()
    note.value = done
  } catch (e) {
    error.value = e
  } finally {
    busy.value = false
  }
}

const save = () => run(() => adminApi.patch(`${b}/products/${pid}`, {
  ...edit.value, brand: edit.value.brand || null, description: edit.value.description || null,
  category_id: edit.value.category_id || null, hsn_sac: edit.value.hsn_sac || null,
}), 'Saved.')
const status = (action) => run(() => adminApi.post(`${b}/products/${pid}/${action}`), `Done: ${action}.`)

// "size=M, color=Black" -> {size: "M", color: "Black"}
const parseOptions = (text) => Object.fromEntries(text.split(',').map((x) => x.split('=').map((y) => y.trim())).filter(([k, v]) => k && v))

function addVariant() {
  const v = variant.value
  run(() => adminApi.post(`${b}/products/${pid}/variants`, {
    sku: v.sku, options: parseOptions(v.options), selling_price: v.selling_price, mrp: v.mrp || null,
    cost_price: v.cost_price || '0', reorder_threshold: v.reorder_threshold || '0',
  }), 'Variant added.').then(() => {
    if (!error.value) variant.value = { sku: '', options: '', selling_price: '', mrp: '', cost_price: '', reorder_threshold: '0' }
  })
}
const patchVariant = (v, body) => run(() => adminApi.patch(`${b}/variants/${v.id}`, body), 'Variant updated.')
function archiveProduct() {
  if (confirm('Archive this product? It disappears from the shop.')) status('archive')
}
function archiveVariant(v) {
  if (confirm(`Archive ${v.sku}?`)) patchVariant(v, { archived: true })
}
function editPrice(v) {
  const price = prompt(`New selling price for ${v.sku}`, v.price)
  if (price) patchVariant(v, { selling_price: price, ...(v.mrp && Number(v.mrp) < Number(price) ? { mrp: price } : {}) })
}
function addOpening() {
  const o = opening.value
  run(() => adminApi.post(`${b}/inventory/opening`, { warehouse_id: o.warehouse_id,
    lines: [{ variant_id: o.variant_id, quantity: o.quantity, ...(o.unit_cost ? { unit_cost: o.unit_cost } : {}) }] }), 'Opening stock recorded.')
}
</script>

<template>
  <p><RouterLink :to="`/b/${route.params.bid}/products`">‹ Products</RouterLink></p>
  <ErrorBox :error="error" :labels="{ hsn_sac: 'HSN/SAC', variants: 'Variants', tax_class: 'GST class' }" />
  <p v-if="note" class="ok">{{ note }}</p>
  <template v-if="p">
    <div class="row between">
      <h1>{{ p.name }} <StatusBadge :status="p.status" /></h1>
      <div v-if="writer && p.status !== 'ARCHIVED'" class="row">
        <button v-if="p.status === 'DRAFT'" :disabled="busy" @click="status('publish')">Publish</button>
        <button v-if="p.status === 'PUBLISHED'" class="ghost" :disabled="busy" @click="status('unpublish')">Unpublish</button>
        <button class="danger" :disabled="busy" @click="archiveProduct">Archive</button>
      </div>
    </div>
    <form class="card stack" @submit.prevent="save">
      <div class="grid3">
        <label>Name <input v-model="edit.name" :disabled="!writer" required maxlength="200" /></label>
        <label>Brand <input v-model="edit.brand" :disabled="!writer" maxlength="100" /></label>
        <label>Category <select v-model="edit.category_id" :disabled="!writer"><option value="">None</option><option v-for="c in categories" :key="c.id" :value="c.id">{{ c.name }}</option></select></label>
        <label>GST class <select v-model="edit.tax_class" :disabled="!writer"><option v-for="t in ['GST_0', 'GST_5', 'GST_18', 'GST_40', 'EXEMPT', 'NONE']" :key="t">{{ t }}</option></select></label>
        <label>HSN/SAC <input v-model="edit.hsn_sac" :disabled="!writer" pattern="[0-9]{4}([0-9]{2}){0,2}" /></label>
        <label>Sold in <select v-model="edit.stock_unit" :disabled="!writer || p.status !== 'DRAFT'"><option v-for="u in ['PCS', 'PACK', 'PAIR', 'BOX', 'DOZEN', 'KG', 'G', 'L', 'ML']" :key="u">{{ u }}</option></select></label>
      </div>
      <label>Description <textarea v-model="edit.description" :disabled="!writer" maxlength="5000" rows="3" /></label>
      <button v-if="writer && p.status !== 'ARCHIVED'" :disabled="busy">Save</button>
    </form>
    <h2>Variants</h2>
    <p v-if="p.stock_unavailable" class="warn small">Stock figures are unavailable right now.</p>
    <table>
      <thead><tr><th>SKU</th><th>Options</th><th class="num">Price</th><th class="num">MRP</th><th v-if="writer" class="num">Cost</th>
        <th class="num">Reorder at</th><th class="num">Available</th><th>State</th><th v-if="writer"></th></tr></thead>
      <tbody><tr v-for="v in p.variants" :key="v.id" :class="v.archived && 'mutedrow'">
        <td>{{ v.sku }}</td><td>{{ options(v.options) }}</td><td class="num">{{ money(v.price) }}</td><td class="num">{{ money(v.mrp) }}</td>
        <td v-if="writer" class="num">{{ money(v.cost_price) }}</td><td class="num">{{ Number(v.reorder_threshold) }}</td>
        <td class="num">{{ v.stock ? Number(v.stock.available) : '—' }}</td>
        <td>{{ v.archived ? 'archived' : v.is_active ? 'active' : 'inactive' }}</td>
        <td v-if="writer && !v.archived" class="row">
          <button class="ghost small" :disabled="busy" @click="editPrice(v)">Price</button>
          <button class="ghost small" :disabled="busy" @click="patchVariant(v, { is_active: !v.is_active })">{{ v.is_active ? 'Deactivate' : 'Activate' }}</button>
          <button class="ghost small" :disabled="busy" @click="archiveVariant(v)">Archive</button>
        </td><td v-else-if="writer"></td></tr></tbody>
    </table>
    <form v-if="writer && p.status !== 'ARCHIVED'" class="card stack" @submit.prevent="addVariant">
      <h3>Add a variant</h3>
      <div class="grid3">
        <label>SKU <input v-model="variant.sku" required pattern="[A-Za-z0-9._\-]{1,60}" /></label>
        <label>Options <input v-model="variant.options" placeholder="size=M, color=Black" /></label>
        <label>Selling price ₹ <input v-model="variant.selling_price" required type="number" min="0.01" step="0.01" /></label>
        <label>MRP ₹ <input v-model="variant.mrp" type="number" min="0.01" step="0.01" /></label>
        <label>Cost ₹ <input v-model="variant.cost_price" type="number" min="0" step="0.01" /></label>
        <label>Reorder at <input v-model="variant.reorder_threshold" type="number" min="0" step="1" /></label>
      </div>
      <button :disabled="busy">Add variant</button>
    </form>
    <form v-if="can('inventory.manage') && p.variants.some((v) => !v.archived)" class="card stack" @submit.prevent="addOpening">
      <h3>Opening stock <small class="muted">(first stock of a variant in a warehouse)</small></h3>
      <div class="grid3">
        <label>Variant <select v-model="opening.variant_id" required><option v-for="v in p.variants.filter((x) => !x.archived)" :key="v.id" :value="v.id">{{ v.sku }}</option></select></label>
        <label>Warehouse <select v-model="opening.warehouse_id" required><option v-for="w in warehouses" :key="w.id" :value="w.id">{{ w.code }} · {{ w.name }}</option></select></label>
        <label>Quantity <input v-model="opening.quantity" required type="number" min="0.001" step="0.001" /></label>
        <label>Unit cost ₹ (blank = variant cost) <input v-model="opening.unit_cost" type="number" min="0" step="0.01" /></label>
      </div>
      <button :disabled="busy">Record opening stock</button>
    </form>
  </template>
</template>
