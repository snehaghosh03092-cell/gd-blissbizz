<script setup>
import { ref } from 'vue'
import { useRoute } from 'vue-router'
import { adminApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import StatusBadge from '../shared/StatusBadge.vue'
import { date, money, options, words } from '../shared/format.js'
import { base, can } from './store.js'

const route = useRoute()
const b = base(route.params.bid)
const order = ref(null)
const error = ref(null)
const busy = ref(false)
const ship = ref({ carrier: '', tracking_number: '' })
const refundMethod = ref('BANK_TRANSFER')
const refundRef = ref('')
const damaged = ref([])

async function run(fn) {
  busy.value = true
  error.value = null
  try {
    const r = await fn()
    order.value = r.items ? r : await adminApi.get(`${b}/orders/${route.params.oid}`)
  } catch (e) {
    error.value = e
  } finally {
    busy.value = false
  }
}
run(() => adminApi.get(`${b}/orders/${route.params.oid}`))

const act = (action, body) => run(() => adminApi.post(`${b}/orders/${route.params.oid}/${action}`, body))
const ret = (rid, action, body) => run(() => adminApi.post(`${b}/returns/${rid}/${action}`, body))
function cancel() {
  const reason = prompt(order.value.payment_status === 'PAID' ? 'Reason (the customer will be refunded in full):' : 'Reason for cancelling:')
  if (reason !== null) act('cancel', { reason: reason || null })
}
function reject(rid) {
  const note = prompt('Why is the return rejected?')
  if (note !== null) ret(rid, 'reject', { note: note || null })
}
const itemName = (id) => order.value.items.find((i) => i.id === id)?.product_name
</script>

<template>
  <p><RouterLink :to="`/b/${route.params.bid}/orders`">‹ Orders</RouterLink></p>
  <ErrorBox :error="error" />
  <template v-if="order">
    <div class="row between">
      <h1>{{ order.order_number }}</h1>
      <div class="row"><StatusBadge :status="order.order_status" /><StatusBadge :status="order.payment_status" /><StatusBadge :status="order.fulfilment_status" /></div>
    </div>
    <p class="muted">Placed {{ date(order.placed_at) }} · {{ order.payment_method === 'COD' ? 'cash on delivery' : 'online payment' }}
      <span v-if="order.cancel_reason"> · cancelled: {{ order.cancel_reason }}</span></p>
    <div v-if="can('sales.fulfil')" class="actions card">
      <button v-if="order.order_status === 'CONFIRMED'" :disabled="busy" @click="act('pack')">Mark packed</button>
      <template v-if="['CONFIRMED', 'PACKED'].includes(order.order_status)">
        <input v-model="ship.carrier" placeholder="Courier" maxlength="60" aria-label="Courier" />
        <input v-model="ship.tracking_number" placeholder="Tracking number" maxlength="80" aria-label="Tracking number" />
        <button :disabled="busy" @click="act('ship', { carrier: ship.carrier || null, tracking_number: ship.tracking_number || null })">Ship (issues invoice)</button>
        <button class="danger" :disabled="busy" @click="cancel">Cancel order</button>
      </template>
      <button v-if="order.order_status === 'PENDING_PAYMENT'" class="danger" :disabled="busy" @click="cancel">Cancel order</button>
      <button v-if="order.order_status === 'SHIPPED'" :disabled="busy" @click="act('deliver')">Mark delivered{{ order.payment_method === 'COD' ? ' (cash collected)' : '' }}</button>
    </div>
    <table>
      <thead><tr><th>Item</th><th>SKU</th><th class="num">Qty</th><th class="num">Price</th><th class="num">Taxable</th><th class="num">GST</th><th class="num">Total</th></tr></thead>
      <tbody>
        <tr v-for="i in order.items" :key="i.id"><td>{{ i.product_name }}<div class="muted small">{{ options(i.options) }}</div></td><td>{{ i.sku }}</td>
          <td class="num">{{ i.quantity }}</td><td class="num">{{ money(i.unit_price) }}</td><td class="num">{{ money(i.taxable_value) }}</td>
          <td class="num">{{ money(Number(i.cgst) + Number(i.sgst) + Number(i.igst) + Number(i.cess)) }}</td><td class="num">{{ money(i.line_total) }}</td></tr>
      </tbody>
      <tfoot>
        <tr><td colspan="6">Shipping</td><td class="num">{{ money(order.shipping_total) }}</td></tr>
        <tr><td colspan="6">GST</td><td class="num">{{ money(order.tax_total) }}</td></tr>
        <tr><td colspan="6">Round off</td><td class="num">{{ money(order.round_off) }}</td></tr>
        <tr class="grand"><td colspan="6">Total</td><td class="num">{{ money(order.grand_total) }}</td></tr>
      </tfoot>
    </table>
    <div class="grid2 top">
      <section class="card">
        <h3>Ship to</h3>
        <p>{{ order.ship_to.recipient }} · {{ order.ship_to.phone }}<br />{{ order.ship_to.line1 }}<span v-if="order.ship_to.line2">, {{ order.ship_to.line2 }}</span>, {{ order.ship_to.city }} {{ order.ship_to.postal_code }} (state {{ order.ship_to.state_code }})</p>
        <p v-for="s in order.shipments" :key="s.shipped_at" class="small">{{ s.carrier || 'Courier' }} {{ s.tracking_number }} · {{ words(s.status) }}</p>
        <h3>Invoice</h3>
        <p v-if="!order.invoice" class="muted small">Issued when the order ships.</p>
        <p v-else-if="order.invoice.pending" class="warn small">Not issued yet (billing was unavailable). <RouterLink :to="`/b/${route.params.bid}/invoices/order-${order.id}`">Issue now</RouterLink></p>
        <p v-else><RouterLink :to="`/b/${route.params.bid}/invoices/${order.invoice.id}`">{{ order.invoice.number }}</RouterLink> · {{ money(order.invoice.grand_total) }}
          <span v-for="n in order.invoice.credit_notes" :key="n.id" class="small"><br />Credit note {{ n.number }} · {{ money(n.grand_total) }}</span></p>
        <h3>Payments</h3>
        <p v-for="p in order.payments" :key="p.provider + p.amount" class="small">{{ p.provider }} · {{ p.method }} · {{ words(p.status) }} · {{ money(p.amount) }}</p>
        <p v-for="r in order.refunds" :key="r.at" class="small">Refund {{ money(r.amount) }} · {{ r.reason }}</p>
        <p v-if="!order.payments?.length" class="muted small">Nothing collected yet.</p>
      </section>
      <section class="card">
        <h3>History</h3>
        <ol class="timeline"><li v-for="(h, n) in order.history" :key="n">{{ words(h.axis) }}: {{ words(h.to) }}
          <span class="muted small">{{ date(h.at) }} · {{ words(h.actor) }}<span v-if="h.note"> · {{ h.note }}</span></span></li></ol>
      </section>
    </div>
    <section v-for="r in order.returns" :key="r.id" class="card">
      <h3>Return <StatusBadge :status="r.status" /></h3>
      <p>{{ r.reason }} · {{ date(r.created_at) }}<span v-if="r.refund"> · refunded {{ money(r.refund) }}</span></p>
      <ul><li v-for="i in r.items" :key="i.id">
        <label v-if="r.status === 'APPROVED'" class="check"><input v-model="damaged" type="checkbox" :value="i.id" /> damaged</label>
        {{ itemName(i.order_item_id) }} × {{ i.quantity }} <span class="muted small">{{ r.status === 'APPROVED' ? '' : words(i.condition) }}</span></li></ul>
      <div class="row">
        <template v-if="can('sales.returns') && r.status === 'REQUESTED'">
          <button :disabled="busy" @click="ret(r.id, 'approve')">Approve</button>
          <button class="danger" :disabled="busy" @click="reject(r.id)">Reject</button>
        </template>
        <button v-if="can('sales.returns') && r.status === 'APPROVED'" :disabled="busy" @click="ret(r.id, 'receive', { damaged_item_ids: damaged })">Goods received</button>
        <template v-if="can('sales.refund') && r.status === 'RECEIVED'">
          <select v-model="refundMethod" aria-label="Refund method"><option value="BANK_TRANSFER">Bank transfer</option><option value="UPI">UPI</option><option value="CASH">Cash</option></select>
          <input v-model="refundRef" placeholder="Reference (optional)" maxlength="60" aria-label="Reference" />
          <button :disabled="busy" @click="ret(r.id, 'refund', { method: refundMethod, reference: refundRef || null })">Refund &amp; issue credit note</button>
        </template>
      </div>
    </section>
  </template>
</template>
