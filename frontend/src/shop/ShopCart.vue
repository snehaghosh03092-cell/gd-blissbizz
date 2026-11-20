<script setup>
import { ref } from 'vue'
import { shopApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import { money, options } from '../shared/format.js'
import { loadCart, state } from './store.js'

const error = ref(null)
const busy = ref(false)
loadCart()

async function setQty(item, q) {
  busy.value = true
  error.value = null
  try {
    state.cart = await shopApi.post('/cart/items', { variant_id: item.variant_id, quantity: Number(Math.max(0, Number(q) || 0).toFixed(3)) })
  } catch (e) {
    error.value = e.code === 'out_of_stock' ? { message: `Only ${Object.values(e.fields)[0]} of ${item.product_name} available.` } : e
    await loadCart()
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <h1>Your cart</h1>
  <ErrorBox :error="error" />
  <p v-if="!state.cart?.items.length" class="empty">Your cart is empty. <RouterLink to="/">Start shopping</RouterLink></p>
  <template v-else>
    <table>
      <thead><tr><th>Item</th><th class="num">Price</th><th>Quantity</th><th class="num">Total</th><th></th></tr></thead>
      <tbody>
        <tr v-for="i in state.cart.items" :key="i.variant_id" :class="i.issue && 'warnrow'">
          <td><RouterLink v-if="i.product_slug" :to="`/p/${i.product_slug}`">{{ i.product_name || 'Unavailable item' }}</RouterLink>
            <div class="muted small">{{ options(i.options) }}</div>
            <div v-if="i.issue === 'unavailable'" class="bad small">No longer sold. Please remove it.</div>
            <div v-if="i.issue === 'insufficient_stock'" class="bad small">Only {{ i.available }} left. Lower the quantity.</div></td>
          <td class="num">{{ money(i.unit_price) }}</td>
          <td><input type="number" class="qty" :value="i.quantity" min="0" max="99" :disabled="busy" aria-label="Quantity"
                     @change="(e) => setQty(i, e.target.value)" /></td>
          <td class="num">{{ money(i.line_total) }}</td>
          <td><button class="ghost" :disabled="busy" @click="setQty(i, 0)">Remove</button></td>
        </tr>
      </tbody>
    </table>
    <div class="summary">
      <p>Subtotal <strong>{{ money(state.cart.subtotal) }}</strong></p>
      <p class="muted small">Shipping and GST are shown at checkout.</p>
      <RouterLink v-if="!state.cart.has_issues" class="button" :to="state.customer ? '/checkout' : '/login?next=/checkout'">Checkout</RouterLink>
      <p v-else class="bad">Fix the highlighted items to continue.</p>
    </div>
  </template>
</template>
