<script setup>
import { ref } from 'vue'
import AdminInsights from './AdminInsights.vue'
import { useRoute } from 'vue-router'
import { adminApi } from '../api/client.js'
import { base, can, session } from './store.js'

const route = useRoute()
const b = base(route.params.bid)
const tiles = ref([])

async function count(label, path, query, link, pick = (r) => r.total) {
  try {
    return { label, value: pick(await adminApi.get(b + path, query)), link }
  } catch {
    return null
  }
}

Promise.all([
  can('sales.read') && count('Orders to pack', '/orders', { status: 'CONFIRMED', page_size: 1 }, 'orders?status=CONFIRMED'),
  can('sales.read') && count('Packed, to ship', '/orders', { status: 'PACKED', page_size: 1 }, 'orders?status=PACKED'),
  can('sales.read') && count('Out for delivery', '/orders', { status: 'SHIPPED', page_size: 1 }, 'orders?status=SHIPPED'),
  can('sales.read') && count('Open returns', '/returns', { status: 'REQUESTED' }, 'returns', (r) => r.items.length),
  can('inventory.read') && count('Low or out of stock', '/inventory', { page_size: 1 }, 'inventory?low=1', (r) => r.summary.low_stock),
  can('purchasing.read') && count('Supplier bills overdue', '/supplier-payments', {}, 'payments', (r) => r.items.filter((p) => p.overdue).length),
]).then((r) => (tiles.value = r.filter(Boolean)))
</script>

<template>
  <AdminInsights v-if="can('reports.read')" />
  <h1 v-else>Overview</h1>
  <p v-if="session.current?.status === 'ONBOARDING'" class="banner">Your shop is not live yet.
    <RouterLink :to="`/b/${route.params.bid}/setup`">Finish setting up ›</RouterLink></p>
  <section v-if="tiles.length" class="operations-summary"><h2>Operations queue</h2><div class="operations-tiles">
    <RouterLink v-for="t in tiles" :key="t.label" :to="`/b/${route.params.bid}/${t.link}`" class="operation-tile">
      <span class="operation-value">{{ t.value }}</span><span class="muted">{{ t.label }}</span>
    </RouterLink>
  </div></section>
</template>
<style scoped>
.operations-summary{margin-top:26px}.operations-summary h2{font-size:14px;color:#7e718e;margin-bottom:14px}.operations-tiles{display:flex;gap:12px;flex-wrap:wrap}.operation-tile{display:flex;gap:10px;align-items:center;background:white;border:1px solid #ebe6f2;border-radius:10px;padding:12px 16px;text-decoration:none;font-size:11px;color:#95879f}.operation-value{font-size:20px;color:#8264af;font-weight:700}
</style>
