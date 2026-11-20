<script setup>
import { ref } from 'vue'
import { useRoute } from 'vue-router'
import { adminApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import InvoiceDoc from '../shared/InvoiceDoc.vue'
import { base } from './store.js'

// /invoices/<id>, or /invoices/order-<order id> to fetch (and if needed issue) an order's invoice.
const route = useRoute()
const b = base(route.params.bid)
const iid = String(route.params.iid)
const invoice = ref(null)
const error = ref(null)
const path = iid.startsWith('order-') ? `${b}/orders/${iid.slice(6)}/invoice` : `${b}/invoices/${iid}`
adminApi.get(path).then((r) => (invoice.value = r)).catch((e) => (error.value = e))
</script>

<template>
  <p class="no-print"><RouterLink :to="`/b/${route.params.bid}/invoices`">‹ Invoices</RouterLink>
    <span v-if="invoice"> · <RouterLink :to="`/b/${route.params.bid}/orders/${invoice.order_id}`">Order</RouterLink></span></p>
  <ErrorBox :error="error" />
  <InvoiceDoc v-if="invoice" :invoice="invoice" />
</template>
