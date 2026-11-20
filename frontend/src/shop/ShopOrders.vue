<script setup>
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { shopApi } from '../api/client.js'
import Pager from '../shared/Pager.vue'
import StatusBadge from '../shared/StatusBadge.vue'
import { date, money } from '../shared/format.js'
import { state } from './store.js'

const route = useRoute()
const router = useRouter()
const data = ref(null)
watch(() => route.query.page, async () => {
  if (!state.customer) return router.replace('/login?next=/orders')
  data.value = await shopApi.get('/orders', { page: route.query.page || 1, page_size: 10 })
}, { immediate: true })
</script>

<template>
  <h1>My orders</h1>
  <p v-if="data && !data.items.length" class="empty">No orders yet.</p>
  <table v-if="data?.items.length">
    <thead><tr><th>Order</th><th>Placed</th><th>Status</th><th>Payment</th><th class="num">Total</th></tr></thead>
    <tbody>
      <tr v-for="o in data.items" :key="o.id">
        <td><RouterLink :to="`/orders/${o.id}`">{{ o.order_number }}</RouterLink></td><td>{{ date(o.placed_at) }}</td>
        <td><StatusBadge :status="o.order_status" /></td><td><StatusBadge :status="o.payment_status" /></td><td class="num">{{ money(o.grand_total) }}</td>
      </tr>
    </tbody>
  </table>
  <Pager v-if="data" :page="data.page" :page-size="data.page_size" :total="data.total" @go="(n) => router.push({ query: { page: n } })" />
</template>
