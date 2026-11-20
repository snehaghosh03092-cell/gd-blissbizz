<script setup>
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { adminApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import Pager from '../shared/Pager.vue'
import StatusBadge from '../shared/StatusBadge.vue'
import { date, options, words } from '../shared/format.js'
import { base, can } from './store.js'

const route = useRoute()
const router = useRouter()
const b = base(route.params.bid)
const data = ref(null)
const warehouses = ref([])
const adjustments = ref([])
const transfers = ref([])
const runs = ref([])
const ledger = ref(null)
const error = ref(null)
const note = ref('')
const busy = ref(false)
const q = ref(route.query.q || '')
const adj = ref(null) // {variant, warehouse_id, mode, value, reason_code, note}
const move = ref(null) // {variant, from, to, quantity}
const wh = ref({ code: '', name: '', state_code: '' })
const whName = computed(() => Object.fromEntries(warehouses.value.map((w) => [w.id, w.code])))
const variantName = computed(() => Object.fromEntries((data.value?.items || []).map((v) => [v.variant_id, `${v.product_name} ${v.sku}`])))

async function load() {
  const query = { q: route.query.q, page: route.query.page || 1, low_only: route.query.low ? 'true' : undefined }
  const tasks = [adminApi.get(b + '/inventory', query).then((r) => (data.value = r)), adminApi.get(b + '/warehouses').then((r) => (warehouses.value = r.items))]
  tasks.push(adminApi.get(b + '/inventory/adjustments').then((r) => (adjustments.value = r.items)).catch(() => {}))
  tasks.push(adminApi.get(b + '/inventory/transfers').then((r) => (transfers.value = r.items)).catch(() => {}))
  tasks.push(adminApi.get(b + '/inventory/reconcile').then((r) => (runs.value = r.items)).catch(() => {}))
  await Promise.all(tasks)
}
load().catch((e) => (error.value = e))

async function run(fn, done) {
  busy.value = true
  error.value = null
  note.value = ''
  try {
    await fn()
    note.value = done
    adj.value = move.value = null
    await load()
  } catch (e) {
    error.value = e
  } finally {
    busy.value = false
  }
}

function startAdjust(v) {
  const first = v.warehouses[0]
  adj.value = { variant: v, warehouse_id: warehouses.value.find((w) => w.code === first?.code)?.id || warehouses.value[0]?.id,
    mode: 'counted_qty', value: '', reason_code: 'CYCLE_COUNT', note: '', post: true }
}
function saveAdjust() {
  const a = adj.value
  run(() => adminApi.post(b + '/inventory/adjustments', { id: crypto.randomUUID(), reason_code: a.reason_code, note: a.note || null, post: a.post,
    lines: [{ variant_id: a.variant.variant_id, warehouse_id: a.warehouse_id, [a.mode]: String(a.value) }] }), a.post ? 'Stock adjusted.' : 'Count saved as a draft.')
}
function startMove(v) {
  move.value = { variant: v, from: warehouses.value[0]?.id, to: warehouses.value[1]?.id, quantity: '' }
}
function saveMove() {
  const m = move.value
  run(() => adminApi.post(b + '/inventory/transfers', { id: crypto.randomUUID(), from_warehouse_id: m.from, to_warehouse_id: m.to,
    lines: [{ variant_id: m.variant.variant_id, quantity: String(m.quantity) }] }), 'Transfer dispatched. Mark it received when it arrives.')
}
async function showLedger(v) {
  ledger.value = { v, rows: (await adminApi.get(b + '/inventory/ledger', { variant_id: v.variant_id, page_size: 50 })).items }
}
const addWarehouse = () => run(() => adminApi.post(b + '/warehouses', { ...wh.value, state_code: wh.value.state_code || null }), 'Warehouse added.')
const qty = (n) => Number(n)
</script>

<template>
  <h1>Inventory</h1>
  <p v-if="data" class="muted">{{ data.summary.variants }} variants · {{ data.summary.low_stock }} low or out · {{ data.summary.out_of_stock }} out of stock</p>
  <form class="filters" @submit.prevent="router.push({ query: { ...route.query, q: q || undefined, page: undefined } })">
    <input v-model="q" placeholder="Product or SKU" aria-label="Search" /><button>Search</button>
    <label class="check"><input type="checkbox" :checked="!!route.query.low" @change="(e) => router.push({ query: { ...route.query, low: e.target.checked ? 1 : undefined, page: undefined } })" /> Low / out only</label>
  </form>
  <ErrorBox :error="error" />
  <p v-if="note" class="ok">{{ note }}</p>

  <form v-if="adj" class="card stack" @submit.prevent="saveAdjust">
    <h3>Adjust {{ adj.variant.product_name }} · {{ adj.variant.sku }}</h3>
    <div class="grid3">
      <label>Warehouse <select v-model="adj.warehouse_id"><option v-for="w in warehouses" :key="w.id" :value="w.id">{{ w.code }}</option></select></label>
      <label>How <select v-model="adj.mode"><option value="counted_qty">I counted (set to)</option><option value="delta">Change by (+/−)</option></select></label>
      <label>{{ adj.mode === 'delta' ? 'Change' : 'Counted' }} <input v-model="adj.value" type="number" step="0.001" required /></label>
      <label>Reason <select v-model="adj.reason_code"><option v-for="r in ['CYCLE_COUNT', 'DAMAGED', 'EXPIRED', 'LOST', 'FOUND', 'CORRECTION', 'OTHER']" :key="r" :value="r">{{ words(r) }}</option></select></label>
      <label>Note <input v-model="adj.note" maxlength="500" /></label>
      <label class="check"><input v-model="adj.post" type="checkbox" /> Apply now (untick to save a draft count)</label>
    </div>
    <div class="row"><button :disabled="busy">Save</button><button type="button" class="ghost" @click="adj = null">Cancel</button></div>
  </form>

  <form v-if="move" class="card stack" @submit.prevent="saveMove">
    <h3>Move {{ move.variant.product_name }} · {{ move.variant.sku }}</h3>
    <div class="grid3">
      <label>From <select v-model="move.from"><option v-for="w in warehouses" :key="w.id" :value="w.id">{{ w.code }}</option></select></label>
      <label>To <select v-model="move.to"><option v-for="w in warehouses" :key="w.id" :value="w.id">{{ w.code }}</option></select></label>
      <label>Quantity <input v-model="move.quantity" type="number" min="0.001" step="0.001" required /></label>
    </div>
    <div class="row"><button :disabled="busy">Dispatch</button><button type="button" class="ghost" @click="move = null">Cancel</button></div>
  </form>

  <table v-if="data">
    <thead><tr><th>Product</th><th>SKU</th><th class="num">On hand</th><th class="num">Held</th><th class="num">Available</th><th>By warehouse</th><th>Status</th><th></th></tr></thead>
    <tbody><tr v-for="v in data.items" :key="v.variant_id">
      <td>{{ v.product_name }}<div class="muted small">{{ options(v.options) }}</div></td><td>{{ v.sku }}</td>
      <td class="num">{{ qty(v.on_hand) }}</td><td class="num">{{ qty(v.reserved) }}</td><td class="num">{{ qty(v.available) }}</td>
      <td class="small">{{ v.warehouses.map((w) => `${w.code} ${qty(w.available)}`).join(' · ') || '—' }}</td>
      <td><StatusBadge :status="v.status" /></td>
      <td class="row">
        <button v-if="can('inventory.adjust')" class="ghost small" @click="startAdjust(v)">Adjust</button>
        <button v-if="can('inventory.transfer') && warehouses.length > 1" class="ghost small" @click="startMove(v)">Move</button>
        <button class="ghost small" @click="showLedger(v)">History</button>
      </td></tr></tbody>
  </table>
  <Pager v-if="data" :page="data.page" :page-size="data.page_size" :total="data.total" @go="(n) => router.push({ query: { ...route.query, page: n } })" />

  <section v-if="ledger" class="card">
    <div class="row between"><h3>History: {{ ledger.v.product_name }} {{ ledger.v.sku }}</h3><button class="ghost" @click="ledger = null">Close</button></div>
    <table><thead><tr><th>When</th><th>Type</th><th>Warehouse</th><th class="num">Change</th><th>Reason</th></tr></thead>
      <tbody><tr v-for="t in ledger.rows" :key="t.id"><td>{{ date(t.created_at) }}</td><td>{{ words(t.type) }}</td><td>{{ t.warehouse }}</td>
        <td class="num">{{ Number(t.quantity) > 0 ? '+' : '' }}{{ t.quantity }}</td><td>{{ words(t.reason) }}</td></tr></tbody></table>
  </section>

  <div class="grid2 top">
    <section class="card">
      <h3>Transfers</h3>
      <p v-if="!transfers.length" class="muted small">None.</p>
      <p v-for="t in transfers.slice(0, 10)" :key="t.id" class="small">
        <StatusBadge :status="t.status" /> {{ whName[t.from_warehouse_id] }} → {{ whName[t.to_warehouse_id] }} ·
        {{ t.lines.map((l) => `${variantName[l.variant_id] || l.variant_id.slice(0, 8)} × ${l.quantity}`).join(', ') }} · {{ date(t.created_at) }}
        <template v-if="t.status === 'IN_TRANSIT' && can('inventory.transfer')">
          <button class="ghost small" :disabled="busy" @click="run(() => adminApi.post(`${b}/inventory/transfers/${t.id}/receive`), 'Transfer received.')">Received</button>
          <button class="ghost small" :disabled="busy" @click="run(() => adminApi.post(`${b}/inventory/transfers/${t.id}/cancel`), 'Transfer cancelled.')">Cancel</button>
        </template>
      </p>
    </section>
    <section class="card">
      <h3>Adjustments</h3>
      <p v-if="!adjustments.length" class="muted small">None.</p>
      <p v-for="a in adjustments.slice(0, 10)" :key="a.id" class="small">
        <StatusBadge :status="a.status" /> {{ words(a.reason_code) }} · {{ a.lines.map((l) => `${variantName[l.variant_id] || l.variant_id.slice(0, 8)} ${Number(l.delta) > 0 ? '+' : ''}${l.delta}`).join(', ') }} · {{ date(a.created_at) }}
        <button v-if="a.status === 'DRAFT' && can('inventory.adjust')" class="ghost small" :disabled="busy" @click="run(() => adminApi.post(`${b}/inventory/adjustments/${a.id}/post`), 'Draft posted.')">Post</button>
      </p>
    </section>
    <section class="card">
      <div class="row between"><h3>Reconciliation</h3>
        <button v-if="can('inventory.reconcile')" class="ghost" :disabled="busy" @click="run(() => adminApi.post(b + '/inventory/reconcile'), 'Check finished.')">Run check</button></div>
      <p class="muted small">Compares every balance with its history and with held orders. It only reports; fix differences with an adjustment.</p>
      <template v-if="runs[0]"><p class="small">Last run {{ date(runs[0].started_at) }}: {{ runs[0].checked }} balances, {{ runs[0].discrepancies.length }} differences.</p>
        <p v-for="(d, n) in runs[0].discrepancies.slice(0, 10)" :key="n" class="small bad">{{ words(d.kind) }} · {{ variantName[d.variant_id] || d.variant_id.slice(0, 8) }} @ {{ whName[d.warehouse_id] }}: expected {{ d.expected }}, found {{ d.actual }}</p></template>
    </section>
    <section class="card">
      <h3>Warehouses</h3>
      <p v-for="w in warehouses" :key="w.id" class="small">{{ w.code }} · {{ w.name }} <span v-if="w.is_default" class="muted">(default: orders ship from here first)</span></p>
      <form v-if="can('inventory.manage')" class="row" @submit.prevent="addWarehouse">
        <input v-model="wh.code" placeholder="Code" required pattern="[A-Za-z0-9_\-]{2,20}" aria-label="Code" />
        <input v-model="wh.name" placeholder="Name" required maxlength="100" aria-label="Name" />
        <button class="ghost" :disabled="busy">Add</button>
      </form>
    </section>
  </div>
</template>
