<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { adminApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import Pager from '../shared/Pager.vue'
import { date, money, words } from '../shared/format.js'
import { base } from './store.js'

const route = useRoute()
const router = useRouter()
const q = ref(route.query.q || '')
const data = ref(null)
const error = ref(null)
adminApi.get(base(route.params.bid) + '/invoices', { q: route.query.q, page: route.query.page || 1 })
  .then((r) => (data.value = r)).catch((e) => (error.value = e))
</script>

<template>
  <h1>Invoices</h1>
  <form class="filters" @submit.prevent="router.push({ query: { q: q || undefined } })">
    <input v-model="q" placeholder="Invoice number" aria-label="Invoice number" /><button>Search</button>
  </form>
  <ErrorBox :error="error" />
  <table v-if="data?.items.length">
    <thead><tr><th>Number</th><th>Type</th><th>Issued</th><th>Buyer</th><th class="num">Taxable</th><th class="num">GST</th><th class="num">Total</th></tr></thead>
    <tbody><tr v-for="i in data.items" :key="i.id">
      <td><RouterLink :to="`/b/${route.params.bid}/invoices/${i.id}`">{{ i.number }}</RouterLink></td><td>{{ words(i.doc_type) }}</td>
      <td>{{ date(i.issued_at) }}</td><td>{{ i.buyer.name }}</td><td class="num">{{ money(i.taxable_total) }}</td>
      <td class="num">{{ money(Number(i.cgst_total) + Number(i.sgst_total) + Number(i.igst_total) + Number(i.cess_total)) }}</td>
      <td class="num">{{ money(i.grand_total) }}</td></tr></tbody>
  </table>
  <p v-else-if="data" class="empty">No invoices yet. They are issued when orders ship.</p>
  <Pager v-if="data" :page="data.page" :page-size="data.page_size" :total="data.total" @go="(n) => router.push({ query: { ...route.query, page: n } })" />
</template>
