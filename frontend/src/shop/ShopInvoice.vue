<script setup>
import { ref } from 'vue'
import { useRoute } from 'vue-router'
import { shopApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import InvoiceDoc from '../shared/InvoiceDoc.vue'

const route = useRoute()
const invoice = ref(null)
const error = ref(null)
shopApi.get(`/orders/${route.params.id}/invoice`).then((r) => (invoice.value = r)).catch((e) => (error.value = e))
</script>

<template>
  <p class="no-print"><RouterLink :to="`/orders/${route.params.id}`">‹ Back to the order</RouterLink></p>
  <ErrorBox :error="error" />
  <InvoiceDoc v-if="invoice" :invoice="invoice" />
</template>
