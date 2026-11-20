<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { adminApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import Pager from '../shared/Pager.vue'
import StatusBadge from '../shared/StatusBadge.vue'
import { date, day, money } from '../shared/format.js'
import { base, can } from './store.js'
import VariantPicker from './VariantPicker.vue'

const route = useRoute()
const router = useRouter()
const b = base(route.params.bid)
const data = ref(null)
const suppliers = ref([])
const error = ref(null)
const creating = ref(false)
const form = ref({ supplier_id: '', expected_date: '', lines: [] })
adminApi.get(b + '/purchase-orders', { status: route.query.status, page: route.query.page || 1 }).then((r) => (data.value = r)).catch((e) => (error.value = e))
adminApi.get(b + '/suppliers').then((r) => (suppliers.value = r.items)).catch(() => {})

function add(v) {
  if (form.value.lines.some((l) => l.variant_id === v.variant_id)) return
  form.value.lines.push({ variant_id: v.variant_id, label: `${v.product_name} · ${v.sku}`, quantity: 1, unit_cost: '' })
}
async function create() {
  error.value = null
  try {
    const po = await adminApi.post(b + '/purchase-orders', { supplier_id: form.value.supplier_id, expected_date: form.value.expected_date || null,
      lines: form.value.lines.map((l) => ({ variant_id: l.variant_id, quantity: String(l.quantity), unit_cost: String(l.unit_cost) })) })
    router.push(`/b/${route.params.bid}/purchase-orders/${po.id}`)
  } catch (e) {
    error.value = e
  }
}
</script>

<template>
  <div class="row between"><h1>Purchase orders</h1><button v-if="can('purchasing.write')" @click="creating = !creating">+ New purchase order</button></div>
  <form v-if="creating" class="card stack" @submit.prevent="create">
    <div class="grid3">
      <label>Supplier <select v-model="form.supplier_id" required><option v-for="s in suppliers" :key="s.id" :value="s.id">{{ s.name }}</option></select></label>
      <label>Expected on <input v-model="form.expected_date" type="date" /></label>
    </div>
    <p v-if="!suppliers.length" class="warn small">Add a supplier first (Suppliers page).</p>
    <VariantPicker :base="b" @pick="add" />
    <table v-if="form.lines.length"><thead><tr><th>Item</th><th>Quantity</th><th>Unit cost ₹</th><th></th></tr></thead>
      <tbody><tr v-for="(l, n) in form.lines" :key="l.variant_id"><td>{{ l.label }}</td>
        <td><input v-model="l.quantity" type="number" min="0.001" step="0.001" required class="qty" /></td>
        <td><input v-model="l.unit_cost" type="number" min="0" step="0.01" required class="qty" /></td>
        <td><button type="button" class="ghost small" @click="form.lines.splice(n, 1)">Remove</button></td></tr></tbody></table>
    <ErrorBox :error="error" />
    <button :disabled="!form.lines.length">Save as draft</button>
  </form>
  <ErrorBox v-if="!creating" :error="error" />
  <table v-if="data">
    <thead><tr><th>Number</th><th>Supplier</th><th>Created</th><th>Expected</th><th>Status</th><th class="num">Total</th></tr></thead>
    <tbody><tr v-for="p in data.items" :key="p.id">
      <td><RouterLink :to="`/b/${route.params.bid}/purchase-orders/${p.id}`">{{ p.number }}</RouterLink></td><td>{{ p.supplier }}</td>
      <td>{{ date(p.created_at) }}</td><td :class="p.overdue && 'bad'">{{ day(p.expected_date) }}{{ p.overdue ? ' (late)' : '' }}</td>
      <td><StatusBadge :status="p.status" /></td><td class="num">{{ money(p.total) }}</td></tr></tbody>
  </table>
  <Pager v-if="data" :page="data.page" :page-size="data.page_size" :total="data.total" @go="(n) => router.push({ query: { ...route.query, page: n } })" />
</template>
