<script setup>
import { ref } from 'vue'
import { useRoute } from 'vue-router'
import { adminApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import StatusBadge from '../shared/StatusBadge.vue'
import { day, money } from '../shared/format.js'
import { base, can } from './store.js'

const route = useRoute()
const b = base(route.params.bid)
const items = ref([])
const status = ref('')
const error = ref(null)
const busy = ref(false)
const load = () => adminApi.get(b + '/supplier-payments', { status: status.value }).then((r) => (items.value = r.items)).catch((e) => (error.value = e))
load()

async function pay(p) {
  const amount = prompt(`Amount paid to ${p.supplier} (outstanding ${p.balance})`, p.balance)
  if (!amount) return
  const reference = prompt('Payment reference (NEFT/UPI/cheque number), optional') || null
  busy.value = true
  error.value = null
  try {
    await adminApi.post(`${b}/supplier-payments/${p.id}/pay`, { amount, reference })
    await load()
  } catch (e) {
    error.value = e
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <h1>Supplier payments</h1>
  <p class="muted small">Created when a supplier bill is approved. Record payments here; nothing is paid automatically.</p>
  <div class="filters"><select v-model="status" aria-label="Status" @change="load"><option value="">All</option><option value="DUE">Due</option><option value="PARTIAL">Part paid</option><option value="PAID">Paid</option></select></div>
  <ErrorBox :error="error" />
  <table v-if="items.length">
    <thead><tr><th>Supplier</th><th>Due</th><th>Status</th><th class="num">Amount</th><th class="num">Paid</th><th class="num">Balance</th><th></th></tr></thead>
    <tbody><tr v-for="p in items" :key="p.id" :class="p.overdue && 'warnrow'"><td>{{ p.supplier }}</td>
      <td :class="p.overdue && 'bad'">{{ day(p.due_date) }}{{ p.overdue ? ' (overdue)' : '' }}</td><td><StatusBadge :status="p.status" /></td>
      <td class="num">{{ money(p.amount) }}</td><td class="num">{{ money(p.amount_paid) }}</td><td class="num">{{ money(p.balance) }}</td>
      <td><button v-if="p.status !== 'PAID' && can('purchasing.approve')" class="ghost small" :disabled="busy" @click="pay(p)">Record payment</button></td></tr></tbody>
  </table>
  <p v-else class="empty">Nothing here.</p>
</template>
