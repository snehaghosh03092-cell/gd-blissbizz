<script setup>
import { ref } from 'vue'
import { useRoute } from 'vue-router'
import { adminApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import { base, can } from './store.js'

const route = useRoute()
const b = base(route.params.bid)
const items = ref([])
const error = ref(null)
const blank = () => ({ name: '', contact_name: '', email: '', phone: '', gstin: '', address: '', lead_time_days: 7, payment_terms_days: 30 })
const form = ref(blank())
const editing = ref(null)
const load = () => adminApi.get(b + '/suppliers').then((r) => (items.value = r.items)).catch((e) => (error.value = e))
load()

const clean = (f) => Object.fromEntries(Object.entries(f).map(([k, v]) => [k, v === '' ? null : v]))
async function save() {
  error.value = null
  try {
    if (editing.value) await adminApi.patch(`${b}/suppliers/${editing.value}`, clean(form.value))
    else await adminApi.post(b + '/suppliers', clean(form.value))
    form.value = blank()
    editing.value = null
    await load()
  } catch (e) {
    error.value = e
  }
}
function edit(s) {
  editing.value = s.id
  form.value = { ...blank(), ...Object.fromEntries(Object.entries(s).map(([k, v]) => [k, v ?? ''])) }
  delete form.value.id
  delete form.value.archived
}
async function archive(s) {
  if (!confirm(`Archive ${s.name}? Existing purchase orders are kept.`)) return
  await adminApi.patch(`${b}/suppliers/${s.id}`, { archived: true })
  await load()
}
</script>

<template>
  <h1>Suppliers</h1>
  <ErrorBox :error="error" :labels="{ gstin: 'GSTIN', name: 'Name' }" />
  <table v-if="items.length">
    <thead><tr><th>Name</th><th>Contact</th><th>GSTIN</th><th class="num">Lead time</th><th class="num">Pay within</th><th></th></tr></thead>
    <tbody><tr v-for="s in items" :key="s.id">
      <td>{{ s.name }}</td><td>{{ s.contact_name }} {{ s.phone }} {{ s.email }}</td><td>{{ s.gstin || '—' }}</td>
      <td class="num">{{ s.lead_time_days }} d</td><td class="num">{{ s.payment_terms_days }} d</td>
      <td v-if="can('purchasing.write')" class="row"><button class="ghost small" @click="edit(s)">Edit</button><button class="ghost small" @click="archive(s)">Archive</button></td></tr></tbody>
  </table>
  <p v-else class="empty">No suppliers yet.</p>
  <form v-if="can('purchasing.write')" class="card stack" @submit.prevent="save">
    <h3>{{ editing ? 'Edit supplier' : 'Add a supplier' }}</h3>
    <div class="grid3">
      <label>Name <input v-model="form.name" required maxlength="150" /></label>
      <label>Contact person <input v-model="form.contact_name" maxlength="150" /></label>
      <label>Phone <input v-model="form.phone" maxlength="32" /></label>
      <label>Email <input v-model="form.email" type="email" maxlength="254" /></label>
      <label>GSTIN <input v-model="form.gstin" maxlength="15" placeholder="optional" /></label>
      <label>Lead time (days) <input v-model.number="form.lead_time_days" type="number" min="0" max="365" /></label>
      <label>Payment terms (days) <input v-model.number="form.payment_terms_days" type="number" min="0" max="365" /></label>
    </div>
    <label>Address <input v-model="form.address" maxlength="1000" /></label>
    <div class="row"><button>{{ editing ? 'Save' : 'Add supplier' }}</button>
      <button v-if="editing" type="button" class="ghost" @click="editing = null; form = blank()">Cancel</button></div>
  </form>
</template>
