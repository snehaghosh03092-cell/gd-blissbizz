<script setup>
import { baseDomain } from '../shared/hosts.js'
import { ref } from 'vue'
import { adminApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import StatusBadge from '../shared/StatusBadge.vue'
import { day, words } from '../shared/format.js'

const summary = ref(null)
const items = ref([])
const q = ref('')
const status = ref('')
const error = ref(null)
const busy = ref(false)

async function load() {
  error.value = null
  try {
    summary.value = await adminApi.get('/platform/summary')
    items.value = (await adminApi.get('/platform/businesses', { q: q.value, status: status.value })).items
  } catch (e) {
    error.value = e
  }
}
load()

async function act(b, action) {
  let body = {}
  if (action === 'suspend') {
    const reason = prompt(`Why suspend ${b.name}? Its shop and admin close immediately.`)
    if (!reason) return
    body = { reason }
  } else if (!confirm(`Reinstate ${b.name}?`)) return
  busy.value = true
  try {
    await adminApi.post(`/platform/businesses/${b.id}/${action}`, body)
    await load()
  } catch (e) {
    error.value = e
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <h1>Businesses</h1>
  <ErrorBox :error="error" />
  <div v-if="summary" class="grid">
    <div class="card tile"><span class="tile-value">{{ summary.businesses }}</span><span class="muted">Businesses</span></div>
    <div v-for="(n, s) in summary.by_status" :key="s" class="card tile"><span class="tile-value">{{ n }}</span><span class="muted">{{ words(s) }}</span></div>
    <div class="card tile"><span class="tile-value">{{ summary.customers }}</span><span class="muted">Customers</span></div>
    <div class="card tile"><span class="muted">Services</span>
      <span v-for="(st, name) in summary.services" :key="name" class="small">{{ name }}: <strong :class="st === 'ready' ? 'ok' : 'bad'">{{ st }}</strong></span></div>
  </div>
  <form class="filters" @submit.prevent="load">
    <input v-model="q" placeholder="Name or web address" aria-label="Search" />
    <select v-model="status" aria-label="Status" @change="load"><option value="">All</option><option value="ACTIVE">Active</option><option value="ONBOARDING">Onboarding</option><option value="SUSPENDED">Suspended</option></select>
    <button>Search</button>
  </form>
  <table>
    <thead><tr><th>Business</th><th>Owner</th><th>Status</th><th>Setup</th><th class="num">Team</th><th class="num">Customers</th><th>Joined</th><th></th></tr></thead>
    <tbody><tr v-for="b in items" :key="b.id">
      <td><strong>{{ b.name }}</strong><div class="muted small">{{ b.slug }}.{{ baseDomain }} · {{ b.category || '—' }} · {{ b.city || '—' }}</div>
        <div v-if="b.suspended_reason" class="bad small">Suspended: {{ b.suspended_reason }}</div></td>
      <td class="small">{{ b.owner_email }}</td><td><StatusBadge :status="b.status" /></td><td class="small">{{ words(b.onboarding_state) }}</td>
      <td class="num">{{ b.members }}</td><td class="num">{{ b.customers }}</td><td class="small">{{ day(b.created_at) }}</td>
      <td><button v-if="b.status !== 'SUSPENDED'" class="danger small" :disabled="busy" @click="act(b, 'suspend')">Suspend</button>
        <button v-else class="small" :disabled="busy" @click="act(b, 'reinstate')">Reinstate</button></td></tr></tbody>
  </table>
</template>
