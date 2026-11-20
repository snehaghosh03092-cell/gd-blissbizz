<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { shopApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import StatusBadge from '../shared/StatusBadge.vue'
import { date, money, options, words } from '../shared/format.js'
import { state } from './store.js'

const route = useRoute()
const order = ref(null)
const error = ref(null)
const busy = ref(false)
const returning = ref(false)
const reason = ref('')
const picks = ref({})

async function load() {
  try {
    order.value = await shopApi.get(`/orders/${route.params.id}`)
  } catch (e) {
    error.value = e
  }
}
watch(() => route.params.id, load, { immediate: true })

const openReturn = computed(() => order.value?.returns.some((r) => ['REQUESTED', 'APPROVED', 'RECEIVED'].includes(r.status)))
const returnable = computed(() => {
  const taken = {}
  for (const r of order.value?.returns || []) if (r.status !== 'REJECTED') for (const i of r.items) taken[i.order_item_id] = (taken[i.order_item_id] || 0) + Number(i.quantity)
  return (order.value?.items || []).map((i) => ({ ...i, left: Number(i.quantity) - (taken[i.id] || 0) })).filter((i) => i.left > 0)
})
const window_days = computed(() => state.shop.policies?.return_window_days)

async function act(fn) {
  busy.value = true
  error.value = null
  try {
    order.value = await fn()
  } catch (e) {
    error.value = e
  } finally {
    busy.value = false
  }
}

function cancel() {
  if (!confirm('Cancel this order?')) return
  act(() => shopApi.post(`/orders/${order.value.id}/cancel`, {}))
}

function requestReturn() {
  const items = Object.entries(picks.value).filter(([, q]) => Number(q) > 0).map(([id, q]) => ({ order_item_id: id, quantity: Number(q) }))
  if (!items.length) return (error.value = { message: 'Choose at least one item and a quantity.' })
  act(() => shopApi.post(`/orders/${order.value.id}/returns`, { reason: reason.value, items })).then(() => {
    if (!error.value) returning.value = false
  })
}
</script>

<template>
  <ErrorBox v-if="!order" :error="error" />
  <template v-if="order">
    <p v-if="route.query.placed" class="ok big">Thank you! Your order is confirmed. Pay {{ money(order.grand_total) }} in cash when it arrives.</p>
    <div class="row between">
      <h1>Order {{ order.order_number }}</h1>
      <div class="row"><StatusBadge :status="order.order_status" /><StatusBadge :status="order.payment_status" /></div>
    </div>
    <p class="muted">Placed {{ date(order.placed_at) }} · {{ order.payment_method === 'COD' ? 'Cash on delivery' : 'Paid online' }}</p>
    <ErrorBox :error="error" />
    <table>
      <thead><tr><th>Item</th><th class="num">Qty</th><th class="num">Price</th><th class="num">Total</th></tr></thead>
      <tbody>
        <tr v-for="i in order.items" :key="i.id"><td>{{ i.product_name }}<div class="muted small">{{ options(i.options) }}</div></td>
          <td class="num">{{ i.quantity }}</td><td class="num">{{ money(i.unit_price) }}</td><td class="num">{{ money(i.line_total) }}</td></tr>
      </tbody>
      <tfoot>
        <tr><td colspan="3">Shipping</td><td class="num">{{ money(order.shipping_total) }}</td></tr>
        <tr><td colspan="3">GST {{ state.shop.business.prices_include_tax ? '(included in prices)' : '' }}</td><td class="num">{{ money(order.tax_total) }}</td></tr>
        <tr class="grand"><td colspan="3">Total</td><td class="num">{{ money(order.grand_total) }}</td></tr>
      </tfoot>
    </table>
    <div class="grid2 top">
      <section class="card">
        <h3>Delivery</h3>
        <p>{{ order.ship_to.recipient }} · {{ order.ship_to.phone }}<br />{{ order.ship_to.line1 }}, {{ order.ship_to.city }} {{ order.ship_to.postal_code }}</p>
        <p v-for="s in order.shipments" :key="s.shipped_at" class="small">{{ s.carrier || 'Courier' }} {{ s.tracking_number }} · shipped {{ date(s.shipped_at) }}<span v-if="s.delivered_at"> · delivered {{ date(s.delivered_at) }}</span></p>
        <RouterLink v-if="order.invoice && !order.invoice.pending" :to="`/orders/${order.id}/invoice`">View invoice {{ order.invoice.number }}</RouterLink>
      </section>
      <section class="card">
        <h3>Progress</h3>
        <ol class="timeline"><li v-for="(h, n) in order.history.filter((x) => x.axis !== 'PAYMENT' || x.to !== 'UNPAID')" :key="n">
          {{ words(h.to) }} <span class="muted small">{{ date(h.at) }}<span v-if="h.note"> · {{ h.note }}</span></span></li></ol>
      </section>
    </div>
    <div class="row">
      <button v-if="order.order_status === 'CONFIRMED'" class="danger" :disabled="busy" @click="cancel">Cancel order</button>
      <button v-if="['DELIVERED', 'RETURNED'].includes(order.order_status) && !openReturn && returnable.length && window_days"
              class="ghost" @click="returning = !returning">Return items</button>
    </div>
    <form v-if="returning" class="card stack" @submit.prevent="requestReturn">
      <h3>Return items</h3>
      <p class="muted small">Returns are accepted within {{ window_days }} days of delivery. {{ state.shop.policies.return_conditions }}</p>
      <label v-for="i in returnable" :key="i.id" class="row">
        <input v-model="picks[i.id]" type="number" min="0" :max="i.left" step="1" class="qty" />
        <span>{{ i.product_name }} ({{ options(i.options) }}) · up to {{ i.left }}</span>
      </label>
      <label>Why are you returning it? <input v-model="reason" required minlength="3" maxlength="255" /></label>
      <button :disabled="busy">Request return</button>
    </form>
    <section v-if="order.returns.length" class="card">
      <h3>Returns</h3>
      <p v-for="r in order.returns" :key="r.id"><StatusBadge :status="r.status" /> {{ r.reason }} · {{ date(r.created_at) }}
        <span v-if="r.refund"> · refunded {{ money(r.refund) }}</span></p>
    </section>
  </template>
</template>
