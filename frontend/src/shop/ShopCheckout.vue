<script setup>
import { onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { shopApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import { money, newKey, options } from '../shared/format.js'
import AddressForm from './AddressForm.vue'
import { loadCart, state } from './store.js'

const router = useRouter()
const addresses = ref([])
const chosen = ref(null)
const preview = ref(null)
const error = ref(null)
const adding = ref(false)
const placing = ref(false)

// One key per checkout attempt, kept until it succeeds: a double click or a retry after a
// network error returns the same order instead of creating a second one.
const KEY = 'checkout-key'
const key = sessionStorage.getItem(KEY) || newKey('chk')
sessionStorage.setItem(KEY, key)

onMounted(async () => {
  if (!state.customer) return router.replace('/login?next=/checkout')
  addresses.value = (await shopApi.get('/me/addresses')).items
  chosen.value = addresses.value.find((a) => a.is_default)?.id || addresses.value[0]?.id || null
  adding.value = !addresses.value.length
})

watch(chosen, async (id) => {
  error.value = null
  try {
    preview.value = await shopApi.post('/checkout/preview', { address_id: id })
  } catch (e) {
    preview.value = null
    error.value = e
  }
})

function saved(address) {
  addresses.value.push(address)
  chosen.value = address.id
  adding.value = false
}

async function place() {
  placing.value = true
  error.value = null
  try {
    const order = await shopApi.post('/checkout', { address_id: chosen.value }, { 'Idempotency-Key': key })
    sessionStorage.removeItem(KEY)
    await loadCart()
    router.replace(`/orders/${order.id}?placed=1`)
  } catch (e) {
    error.value = e
    if (['out_of_stock', 'cart_changed', 'cart_empty'].includes(e.code)) {
      sessionStorage.removeItem(KEY) // a different cart next time: a fresh key
      await loadCart()
    }
  } finally {
    placing.value = false
  }
}
</script>

<template>
  <h1>Checkout</h1>
  <div class="grid2 top">
    <section>
      <h3>Deliver to</h3>
      <label v-for="a in addresses" :key="a.id" class="card radio">
        <input v-model="chosen" type="radio" :value="a.id" />
        <span><strong>{{ a.recipient }}</strong> · {{ a.phone }}<br />{{ a.line1 }}<span v-if="a.line2">, {{ a.line2 }}</span>, {{ a.city }}, {{ a.state }} {{ a.postal_code }}</span>
      </label>
      <AddressForm v-if="adding" @saved="saved" @cancel="adding = false" />
      <button v-else-if="addresses.length < 10" class="ghost" @click="adding = true">+ Add a new address</button>
    </section>
    <section class="card">
      <h3>Order summary</h3>
      <p v-if="!preview && !error" class="muted">Choose an address to see the total.</p>
      <template v-if="preview">
        <div v-for="i in preview.items" :key="i.variant_id" class="row between small">
          <span>{{ i.product_name }} × {{ i.quantity }}<br /><span class="muted">{{ options(i.options) }}</span></span><span>{{ money(i.line_total) }}</span>
        </div>
        <hr />
        <div class="row between"><span>Items</span><span>{{ money(preview.subtotal) }}</span></div>
        <div class="row between"><span>Shipping</span><span>{{ Number(preview.shipping_total) ? money(preview.shipping_total) : 'Free' }}</span></div>
        <div class="row between muted small"><span>GST {{ preview.prices_include_tax ? '(included)' : '' }} {{ preview.inter_state ? 'IGST' : 'CGST + SGST' }}</span><span>{{ money(preview.tax_total) }}</span></div>
        <div v-if="Number(preview.round_off)" class="row between muted small"><span>Round off</span><span>{{ money(preview.round_off) }}</span></div>
        <div class="row between grand"><span>To pay on delivery</span><span>{{ money(preview.grand_total) }}</span></div>
        <p class="muted small">Payment: cash on delivery.</p>
        <button :disabled="placing || !chosen" @click="place">{{ placing ? 'Placing order…' : 'Place order' }}</button>
      </template>
      <ErrorBox :error="error" />
      <RouterLink v-if="error" to="/cart">Back to cart</RouterLink>
    </section>
  </div>
</template>
