<script setup>
import { ref } from 'vue'
import { adminApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import { date } from '../shared/format.js'

const items = ref([])
const error = ref(null)
adminApi.get('/platform/audit', { limit: 200 }).then((r) => (items.value = r.items)).catch((e) => (error.value = e))
</script>

<template>
  <h1>Audit log</h1>
  <p class="muted">Append-only: every suspension, reinstatement, sign-up and go-live, with who and when.</p>
  <ErrorBox :error="error" />
  <table>
    <thead><tr><th>When</th><th>Action</th><th>Business</th><th>By</th><th>Details</th></tr></thead>
    <tbody><tr v-for="e in items" :key="e.id"><td class="small">{{ date(e.at) }}</td><td>{{ e.action }}</td>
      <td class="small">{{ e.business_id?.slice(0, 8) }}</td><td class="small">{{ e.actor_type.toLowerCase() }}</td>
      <td class="small">{{ e.after ? JSON.stringify(e.after) : '' }}</td></tr></tbody>
  </table>
</template>
