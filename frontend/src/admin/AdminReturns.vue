<script setup>
import { ref } from 'vue'
import { useRoute } from 'vue-router'
import { adminApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import StatusBadge from '../shared/StatusBadge.vue'
import { date } from '../shared/format.js'
import { base } from './store.js'

const route = useRoute()
const status = ref('')
const data = ref(null)
const error = ref(null)
const load = () => adminApi.get(base(route.params.bid) + '/returns', { status: status.value }).then((r) => (data.value = r)).catch((e) => (error.value = e))
load()
</script>

<template>
  <h1>Returns</h1>
  <div class="filters"><select v-model="status" aria-label="Status" @change="load">
    <option value="">All</option><option v-for="s in ['REQUESTED', 'APPROVED', 'RECEIVED', 'REFUNDED', 'REJECTED']" :key="s" :value="s">{{ s.toLowerCase() }}</option>
  </select></div>
  <ErrorBox :error="error" />
  <table v-if="data?.items.length">
    <thead><tr><th>Order</th><th>Requested</th><th>Reason</th><th>Status</th></tr></thead>
    <tbody><tr v-for="r in data.items" :key="r.id">
      <td><RouterLink :to="`/b/${route.params.bid}/orders/${r.order_id}`">{{ r.order_number }}</RouterLink></td>
      <td>{{ date(r.created_at) }}</td><td>{{ r.reason }}</td><td><StatusBadge :status="r.status" /></td></tr></tbody>
  </table>
  <p v-else-if="data" class="empty">No returns.</p>
</template>
