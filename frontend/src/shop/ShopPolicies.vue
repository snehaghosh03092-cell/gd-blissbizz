<script setup>
import { state } from './store.js'
const p = state.shop.policies
</script>

<template>
  <h1>Policies</h1>
  <dl class="facts">
    <div v-if="p.shipping_days_min"><dt>Delivery</dt><dd>{{ p.shipping_days_min }}–{{ p.shipping_days_max }} days</dd></div>
    <div v-if="p.free_shipping_over"><dt>Free shipping</dt><dd>On orders of ₹{{ Number(p.free_shipping_over) }} or more (otherwise ₹{{ Number(p.flat_shipping_fee || 0) }})</dd></div>
    <div><dt>Returns</dt><dd>{{ p.return_window_days ? `Within ${p.return_window_days} days of delivery` : 'Not accepted' }}</dd></div>
    <div v-if="p.cancel_window_hours"><dt>Cancelling</dt><dd>Within {{ p.cancel_window_hours }} hours, before the order is packed</dd></div>
    <div v-if="p.support_email || p.support_phone"><dt>Help</dt><dd>{{ p.support_email }} {{ p.support_phone }}</dd></div>
  </dl>
  <article v-for="d in state.shop.documents" :key="d.slug" class="card"><h3>{{ d.title }}</h3><p>{{ d.body }}</p></article>
</template>
