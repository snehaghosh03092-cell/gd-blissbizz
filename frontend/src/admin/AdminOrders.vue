<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { adminApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import Pager from '../shared/Pager.vue'
import StatusBadge from '../shared/StatusBadge.vue'
import { date, money } from '../shared/format.js'
import { base } from './store.js'

const route = useRoute()
const router = useRouter()
const data = ref(null)
const error = ref(null)
const q = ref(route.query.q || '')
const STATUSES = ['', 'PENDING_PAYMENT', 'CONFIRMED', 'PACKED', 'SHIPPED', 'DELIVERED', 'RETURN_REQUESTED', 'RETURNED', 'CANCELLED']
adminApi.get(base(route.params.bid) + '/orders', { status: route.query.status, q: route.query.q, page: route.query.page || 1 })
  .then((r) => (data.value = r)).catch((e) => (error.value = e))
const go = (query) => router.push({ query: { ...route.query, page: undefined, ...query } })
</script>

<template>
  <h1>Orders</h1>
  <form class="filters" @submit.prevent="go({ q: q || undefined })">
    <select :value="route.query.status || ''" aria-label="Status" @change="(e) => go({ status: e.target.value || undefined })">
      <option v-for="s in STATUSES" :key="s" :value="s">{{ s ? s.toLowerCase().replaceAll('_', ' ') : 'All statuses' }}</option>
    </select>
    <input v-model="q" placeholder="Order number, e.g. TC-2611" aria-label="Order number" /><button>Search</button>
  </form>
  <ErrorBox :error="error" />
  <table v-if="data">
    <thead><tr><th>Order</th><th>Placed</th><th>Customer</th><th>Status</th><th>Payment</th><th class="num">Total</th></tr></thead>
    <tbody>
      <tr v-for="o in data.items" :key="o.id">
        <td><RouterLink :to="`/b/${route.params.bid}/orders/${o.id}`">{{ o.order_number }}</RouterLink></td>
        <td>{{ date(o.placed_at) }}</td><td>{{ o.ship_to?.recipient }} · {{ o.ship_to?.city }}</td>
        <td><StatusBadge :status="o.order_status" /></td><td><StatusBadge :status="o.payment_status" /> <small class="muted">{{ o.payment_method }}</small></td>
        <td class="num">{{ money(o.grand_total) }}</td>
      </tr>
    </tbody>
  </table>
  <p v-if="data && !data.items.length" class="empty">No orders match.</p>
  <Pager v-if="data" :page="data.page" :page-size="data.page_size" :total="data.total" @go="(n) => router.push({ query: { ...route.query, page: n } })" />
</template>
