<script setup>
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { adminApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import StatusBadge from '../shared/StatusBadge.vue'
import { date, day, money, newKey } from '../shared/format.js'
import { base, can } from './store.js'

const route = useRoute()
const b = base(route.params.bid)
const path = `${b}/purchase-orders/${route.params.poid}`
const po = ref(null)
const names = ref({})
const warehouses = ref([])
const receive = ref({ warehouse_id: '', qty: {} })
const error = ref(null)
const note = ref('')
const busy = ref(false)
let receiptKey = newKey('grn') // one key per receipt attempt: a double click receives once

async function load() {
  po.value = await adminApi.get(path)
  const missing = po.value.items.filter((i) => !names.value[i.variant_id])
  if (missing.length) {
    const inv = await adminApi.get(b + '/inventory', { page_size: 100 })
    for (const v of inv.items) names.value[v.variant_id] = `${v.product_name} · ${v.sku}`
  }
}
load().catch((e) => (error.value = e))
adminApi.get(b + '/warehouses').then((r) => {
  warehouses.value = r.items
  receive.value.warehouse_id = r.items.find((w) => w.is_default)?.id || ''
})

const receivable = computed(() => ['APPROVED', 'PARTIALLY_RECEIVED'].includes(po.value?.status))
async function run(fn, done) {
  busy.value = true
  error.value = null
  note.value = ''
  try {
    await fn()
    await load()
    note.value = done
  } catch (e) {
    error.value = e
  } finally {
    busy.value = false
  }
}
function fillAll() {
  for (const i of po.value.items) receive.value.qty[i.id] = Number(i.outstanding)
}
function doReceive() {
  const lines = Object.entries(receive.value.qty).filter(([, q]) => Number(q) > 0).map(([po_item_id, q]) => ({ po_item_id, quantity: String(q) }))
  if (!lines.length) return (error.value = { message: 'Enter the quantities that arrived.' })
  run(() => adminApi.post(`${path}/receipts`, { idempotency_key: receiptKey, warehouse_id: receive.value.warehouse_id, lines }), 'Goods received into stock.').then(() => {
    if (!error.value) {
      receiptKey = newKey('grn')
      receive.value.qty = {}
    }
  })
}
function cancelPo() {
  if (confirm('Cancel this purchase order?')) run(() => adminApi.post(`${path}/cancel`), 'Cancelled.')
}
</script>

<template>
  <p><RouterLink :to="`/b/${route.params.bid}/purchase-orders`">‹ Purchase orders</RouterLink></p>
  <ErrorBox :error="error" />
  <p v-if="note" class="ok">{{ note }}</p>
  <template v-if="po">
    <div class="row between"><h1>{{ po.number }} <StatusBadge :status="po.status" /></h1>
      <div class="row">
        <button v-if="po.status === 'DRAFT' && can('purchasing.approve')" :disabled="busy" @click="run(() => adminApi.post(`${path}/approve`), 'Approved.')">Approve</button>
        <button v-if="['DRAFT', 'APPROVED'].includes(po.status) && can('purchasing.write')" class="danger" :disabled="busy" @click="cancelPo">Cancel</button>
      </div></div>
    <p class="muted">{{ po.supplier }} · created {{ date(po.created_at) }} · expected {{ day(po.expected_date) }}<span v-if="po.overdue" class="bad"> (late)</span> · total {{ money(po.total) }}</p>
    <p v-if="po.status === 'DRAFT'" class="muted small">Drafts can be changed; approve to start receiving.</p>
    <table>
      <thead><tr><th>Item</th><th class="num">Ordered</th><th class="num">Unit cost</th><th class="num">Received</th><th class="num">Outstanding</th><th v-if="receivable && can('purchasing.receive')">Arrived now</th></tr></thead>
      <tbody><tr v-for="i in po.items" :key="i.id"><td>{{ names[i.variant_id] || i.variant_id }}</td><td class="num">{{ Number(i.quantity) }}</td>
        <td class="num">{{ money(i.unit_cost) }}</td><td class="num">{{ Number(i.received_quantity) }}</td><td class="num">{{ Number(i.outstanding) }}</td>
        <td v-if="receivable && can('purchasing.receive')"><input v-model="receive.qty[i.id]" type="number" min="0" :max="Number(i.outstanding)" step="0.001" class="qty" :disabled="!Number(i.outstanding)" /></td></tr></tbody>
    </table>
    <div v-if="receivable && can('purchasing.receive')" class="card row">
      <label>Into warehouse <select v-model="receive.warehouse_id"><option v-for="w in warehouses" :key="w.id" :value="w.id">{{ w.code }} · {{ w.name }}</option></select></label>
      <button type="button" class="ghost" @click="fillAll">Everything outstanding</button>
      <button :disabled="busy" @click="doReceive">Receive goods</button>
    </div>
    <section v-if="po.receipts.length" class="card"><h3>Goods receipts</h3>
      <p v-for="r in po.receipts" :key="r.id" class="small">{{ r.number }} · {{ date(r.received_at) }}</p></section>
  </template>
</template>
