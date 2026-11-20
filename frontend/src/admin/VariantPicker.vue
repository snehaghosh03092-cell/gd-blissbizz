<script setup>
import { ref } from 'vue'
import { adminApi } from '../api/client.js'
import { options } from '../shared/format.js'

const props = defineProps({ base: { type: String, required: true } })
const emit = defineEmits(['pick'])
const q = ref('')
const found = ref([])
let timer = null

function search() {
  clearTimeout(timer)
  timer = setTimeout(async () => {
    if (q.value.trim().length < 2) return (found.value = [])
    found.value = (await adminApi.get(props.base + '/inventory', { q: q.value.trim(), page_size: 10 })).items
  }, 250)
}
function pick(v) {
  emit('pick', v)
  q.value = ''
  found.value = []
}
</script>

<template>
  <div class="picker">
    <input v-model="q" placeholder="Find a product or SKU to add…" aria-label="Find a product" @input="search" />
    <ul v-if="found.length" class="dropdown">
      <li v-for="v in found" :key="v.variant_id"><button type="button" class="ghost" @click="pick(v)">
        {{ v.product_name }} · {{ v.sku }} <span class="muted small">{{ options(v.options) }} · {{ Number(v.available) }} available</span></button></li>
    </ul>
  </div>
</template>
