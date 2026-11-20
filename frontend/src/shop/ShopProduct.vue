<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { shopApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import StatusBadge from '../shared/StatusBadge.vue'
import { money } from '../shared/format.js'
import { state } from './store.js'
import { initialVariant } from './variants.js'

const route = useRoute()
const product = ref(null)
const error = ref(null)
const chosen = ref({})
const quantity = ref(1)
const busy = ref(false)
const added = ref('')
const fractional = computed(() => ['KG', 'L'].includes(product.value?.stock_unit))

async function load() {
  error.value = null
  product.value = null
  try {
    product.value = await shopApi.get(`/products/${route.params.slug}`)
    const first = initialVariant(product.value.variants, route.query.variant)
    chosen.value = { ...(first?.options || {}) }
  } catch (e) {
    error.value = e
  }
}
watch(() => [route.params.slug, route.query.variant], load, { immediate: true })

// option name -> all values, in variant order
const optionValues = computed(() => {
  const out = {}
  for (const v of product.value?.variants || []) for (const [k, val] of Object.entries(v.options)) (out[k] ||= []).includes(val) || out[k].push(val)
  return out
})
const variant = computed(() => product.value?.variants.find((v) => Object.entries(chosen.value).every(([k, val]) => v.options[k] === val)) || (product.value?.variants.length === 1 ? product.value.variants[0] : null))

async function add() {
  busy.value = true
  error.value = null
  added.value = ''
  try {
    const existing = state.cart?.items.find((i) => i.variant_id === variant.value.id)
    // round to 3 decimals: 0.1 + 0.2 must be sent as 0.3, not 0.30000000000000004
    const total = Number((Number(quantity.value) + (existing ? Number(existing.quantity) : 0)).toFixed(3))
    state.cart = await shopApi.post('/cart/items', { variant_id: variant.value.id, quantity: total })
    added.value = `Added. You have ${total} in your cart.`
  } catch (e) {
    error.value = e.code === 'out_of_stock' ? { message: `Only ${Object.values(e.fields)[0]} available.` } : e
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <ErrorBox v-if="!product" :error="error" />
  <div v-if="product" class="detail">
    <div class="thumb big">{{ product.name.slice(0, 1) }}</div>
    <div>
      <p class="muted"><RouterLink to="/">Shop</RouterLink> › {{ product.category }}</p>
      <h1>{{ product.name }}</h1>
      <p class="muted">{{ product.brand }}</p>
      <p class="price big">{{ variant ? money(variant.price) : money(product.price_min) }}
        <span v-if="Number(variant?.mrp) > Number(variant?.price)" class="muted small">MRP <s>{{ money(variant.mrp) }}</s></span></p>
      <p v-if="product.price_min !== product.price_max" class="muted small">Prices vary by size/colour: {{ money(product.price_min) }} – {{ money(product.price_max) }}. The price above is for your selected options.</p>
      <p v-if="product.description">{{ product.description }}</p>
      <div v-for="(vals, key) in optionValues" :key="key" class="optgroup">
        <span class="label">{{ key }}</span>
        <button v-for="val in vals" :key="val" :class="['chip', chosen[key] === val && 'on']" @click="chosen = { ...chosen, [key]: val }">{{ val }}</button>
      </div>
      <p v-if="variant"><StatusBadge :status="variant.availability" />
        <span v-if="variant.available_quantity" class="muted"> {{ variant.available_quantity }} left</span></p>
      <p v-else class="muted">This combination is not available.</p>
      <div class="row">
        <input v-model="quantity" type="number" :min="fractional ? 0.1 : 1" :step="fractional ? 0.1 : 1" max="99" class="qty" aria-label="Quantity" />
        <span class="muted">{{ product.stock_unit }}</span>
        <button :disabled="busy || !variant || !variant.in_stock || !(quantity > 0)" @click="add">{{ variant && !variant.in_stock ? 'Out of stock' : 'Add to cart' }}</button>
      </div>
      <p v-if="added" class="ok">{{ added }} <RouterLink to="/cart">Go to cart</RouterLink></p>
      <ErrorBox :error="error" />
    </div>
  </div>
</template>
