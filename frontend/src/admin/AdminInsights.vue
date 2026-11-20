<script setup>
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { adminApi } from '../api/client.js'
import AppIcon from '../shared/AppIcon.vue'
import ErrorBox from '../shared/ErrorBox.vue'
import { money, options } from '../shared/format.js'
import { storefrontUrl } from '../shared/hosts.js'
import { base, can, session } from './store.js'

const route = useRoute()
const b = base(route.params.bid)
const dash = ref(null)
const recs = ref([])
const error = ref(null)
const recError = ref(null)
const loading = ref(true)
const busy = ref('')
const discountPreview = ref(null)
const appliedMessage = ref('')
const appliedLink = ref('')
const kind = ref('')
const expanded = ref(false)
const hovered = ref(null)
const updated = ref(null)
const palette = ['#8555e9', '#32b8cf', '#536de8', '#c16de0', '#55b995', '#e9ad57']
const compact = (value, currency = false) => new Intl.NumberFormat('en-IN', {
  ...(currency ? { style: 'currency', currency: 'INR' } : {}), notation: 'compact', maximumFractionDigits: 2,
}).format(Number(value || 0))
const dateLabel = (v) => new Date(v + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })
async function load() {
  error.value = null; recError.value = null; loading.value = true
  await Promise.all([
    adminApi.get(`${b}/insights/dashboard`).then((r) => { dash.value = r; updated.value = new Date() }).catch((e) => { error.value = e }),
    can('insights.read') ? adminApi.get(`${b}/insights/recommendations`).then((r) => { recs.value = r.items }).catch((e) => { recError.value = e }) : Promise.resolve(),
  ])
  loading.value = false
}
load()
async function decide(r, action) {
  busy.value = r.id; recError.value = null; appliedMessage.value = ''; appliedLink.value = ''
  try {
    const body = action === 'postpone' ? { snooze_days: 7 } : action === 'accept' && hasDiscount(r) ? { expected_price: String(r.evidence.unit_price), discount_pct: String(r.suggested_discount_pct) } : {}
    const result = await adminApi.post(`${b}/insights/recommendations/${r.id}/${action}`, body)
    if (result.discount_application) {
      const label = options(r.variant?.options) || r.evidence?.sku || 'the selected option'
      appliedMessage.value = `Discount applied only to ${r.variant?.product_name || 'this product'} (${label}). New selling price: ${money(result.discount_application.selling_price)}. Other sizes and colours keep their prices.`
      appliedLink.value = variantLink(r)
    }
    discountPreview.value = null
    recs.value = recs.value.filter((item) => item.id !== r.id)
  } catch (e) { recError.value = e } finally { busy.value = '' }
}
function variantLink(r) {
  if (!session.current?.slug || !r.variant?.product_slug || !r.variant_id) return ''
  return `${storefrontUrl(session.current.slug)}/p/${encodeURIComponent(r.variant.product_slug)}?variant=${encodeURIComponent(r.variant_id)}`
}
const hasDiscount = (r) => r.type === 'DISCOUNT' && Number(r.suggested_discount_pct) > 0 && Number(r.evidence?.unit_price) > 0
const discountedPrice = (r) => {
  const cents = Math.round(Number(r.evidence.unit_price) * 100)
  const discountBasisPoints = Math.round(Number(r.suggested_discount_pct) * 100)
  return (Math.round(cents * (10000 - discountBasisPoints) / 10000) / 100).toFixed(2)
}
function ask(text) {
  session.guidePrompt = { text, id: Date.now() }
  session.guideOpen = true
}
const filtered = computed(() => recs.value.filter((r) => !kind.value || r.type === kind.value))
const visibleRecs = computed(() => filtered.value.slice(0, expanded.value ? 20 : 4))
const cards = computed(() => [
  { key: 'revenue', label: 'Total revenue', icon: 'payments', currency: true },
  { key: 'orders', label: 'Orders received', icon: 'orders' },
  { key: 'units', label: 'Units sold', icon: 'products' },
  { key: 'aov', label: 'Average order value', icon: 'shop', currency: true },
])
const maxRevenue = computed(() => Math.max(1, ...(dash.value?.trend || []).map((d) => Number(d.revenue))))
const trendPoints = computed(() => (dash.value?.trend || []).map((d, i, rows) => `${54 + i * 680 / Math.max(1, rows.length - 1)},${204 - Number(d.revenue) / maxRevenue.value * 166}`).join(' '))
const area = computed(() => `M54,204 L${trendPoints.value.replaceAll(' ', ' L')} L734,204 Z`)
const trendTotal = computed(() => (dash.value?.trend || []).reduce((total, row) => total + Number(row.revenue), 0))
const categories = computed(() => {
  const all = dash.value?.categories || []
  const top = all.slice(0, 5)
  if (all.length > 5) top.push({ name: 'Other', revenue: all.slice(5).reduce((n, c) => n + Number(c.revenue), 0) })
  const total = all.reduce((n, c) => n + Number(c.revenue), 0)
  let angle = 0
  return top.map((c, i) => { const share = total ? Number(c.revenue) / total * 100 : 0; const start = angle; angle += share; return { ...c, share, start, end: angle, color: palette[i] } })
})
const ring = computed(() => categories.value.length && categories.value.some((c) => c.share) ? `conic-gradient(${categories.value.map((c) => `${c.color} ${c.start}% ${c.end}%`).join(',')})` : '#eeeaf5')
const recLabels = { REORDER: 'Restock', DISCOUNT: 'Surplus', DEAD_STOCK: 'Slow moving', REVIEW: 'Review' }
</script>

<template>
  <main class="analytics-dashboard">
    <header class="analytics-heading">
      <div><p class="analytics-eyebrow">YOUR BUSINESS AT A GLANCE</p><h1>Business overview<span class="live-dot" title="Connected to your shop data"></span></h1><p>Understand what’s growing. Focus on what needs you.</p></div>
      <div class="analytics-toolbar"><span v-if="dash" class="period-pill"><AppIcon name="setup" :size="16" />{{ dateLabel(dash.period.from) }} – {{ dateLabel(dash.period.to) }}</span><button class="analytics-refresh" :disabled="loading" aria-label="Refresh dashboard" @click="load"><AppIcon name="returns" :size="18" /></button></div>
    </header>
    <ErrorBox :error="error" />
    <div v-if="loading && !dash" class="analytics-loading" role="status">Loading your business performance…</div>
    <template v-if="dash">
      <div class="analytics-period"><span class="analytics-period-active">Last 30 days</span><span>Compared with the previous 30 days</span><small v-if="updated">Updated {{ updated.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' }) }}</small></div>
      <section class="analytics-kpis" aria-label="Business performance indicators">
        <article v-for="k in cards" :key="k.key" class="metric-card">
          <div class="metric-label"><span>{{ k.label }}</span><span class="metric-icon"><AppIcon :name="k.icon" :size="18" /></span></div>
          <strong :title="k.currency ? money(dash.kpis[k.key].value) : String(dash.kpis[k.key].value)">{{ compact(dash.kpis[k.key].value, k.currency) }}</strong>
          <div class="metric-footer"><span v-if="dash.kpis[k.key].change_pct !== null" :class="['metric-change', Number(dash.kpis[k.key].change_pct) < 0 ? 'negative' : 'positive']">{{ Number(dash.kpis[k.key].change_pct) < 0 ? '↘' : '↗' }} {{ Math.abs(dash.kpis[k.key].change_pct) }}%</span><span v-else class="metric-baseline">No prior-period baseline</span><small v-if="dash.kpis[k.key].change_pct !== null">vs. previous period</small></div>
        </article>
      </section>
      <div class="analytics-layout">
        <div class="analytics-main">
          <section class="analytics-panel revenue-panel">
            <div class="panel-heading"><div><h2>Revenue performance</h2><p>Daily sales across your shop</p></div><span class="chart-legend"><i></i> Revenue</span></div>
            <div class="revenue-summary"><strong>{{ money(hovered ? hovered.revenue : trendTotal) }}</strong><span>{{ hovered ? `${dateLabel(hovered.date)} · ${hovered.orders} orders` : 'Total over the last 30 days' }}</span></div>
            <div class="revenue-chart">
              <svg viewBox="0 0 760 242" role="img" :aria-label="`Daily revenue from ${dash.period.from} to ${dash.period.to}; total ${money(trendTotal)}`">
                <defs><linearGradient id="revenue-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#9864ee" stop-opacity=".23"/><stop offset="100%" stop-color="#9864ee" stop-opacity=".01"/></linearGradient></defs>
                <g v-for="tick in [0, 1, 2, 3]" :key="tick"><line x1="54" x2="734" :y1="204 - tick * 55.33" :y2="204 - tick * 55.33" stroke="#edeaf3" stroke-dasharray="4 5"/><text x="43" :y="208 - tick * 55.33" text-anchor="end">{{ compact(maxRevenue * tick / 3, true) }}</text></g>
                <path :d="area" fill="url(#revenue-fill)"/><polyline :points="trendPoints" fill="none" stroke="#8956df" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>
                <g v-for="(d, i) in dash.trend" :key="d.date"><rect :x="45 + i * 680 / Math.max(1, dash.trend.length - 1)" y="24" width="23" height="184" fill="transparent" tabindex="0" :aria-label="`${dateLabel(d.date)}: ${money(d.revenue)}, ${d.orders} orders`" @mouseenter="hovered = d" @mouseleave="hovered = null" @focus="hovered = d" @blur="hovered = null"><title>{{ dateLabel(d.date) }}: {{ money(d.revenue) }}</title></rect></g>
                <text v-for="i in [0, 7, 14, 21, 29]" :key="i" :x="54 + i * 680 / 29" y="232" :text-anchor="i === 0 ? 'start' : i === 29 ? 'end' : 'middle'">{{ dash.trend[i] ? dateLabel(dash.trend[i].date) : '' }}</text>
              </svg>
            </div>
          </section>
          <div class="analytics-secondary">
            <section class="analytics-panel category-panel"><div class="panel-heading"><div><h2>Sales mix</h2><p>Revenue by product category</p></div><AppIcon name="products" :size="18" /></div>
              <div v-if="categories.length" class="category-content"><div class="category-ring" :style="{ background: ring }" role="img" :aria-label="categories.map((c) => `${c.name}: ${c.share.toFixed(1)}%`).join(', ')"><div><strong>{{ dash.categories.length }}</strong><span>categories</span></div></div><ul class="category-legend"><li v-for="c in categories" :key="c.name"><i :style="{ background: c.color }"></i><span :title="c.name">{{ c.name }}</span><strong>{{ c.share.toFixed(1) }}%</strong></li></ul></div><p v-else class="analytics-empty">Your category mix will appear after your first sale.</p>
            </section>
            <section class="analytics-panel health-panel"><div class="panel-heading"><div><h2>Shop health</h2><p>A pulse on stock and customers</p></div><AppIcon name="inventory" :size="18" /></div>
              <div class="health-stat"><span>Inventory at cost</span><strong>{{ compact(dash.stock.inventory_value, true) }}</strong><small :title="money(dash.stock.inventory_value)">{{ money(dash.stock.inventory_value) }} on hand</small></div>
              <div class="health-pair"><div><span class="health-number warning">{{ dash.stock.low_or_out }}</span><span>Low / out of stock</span></div><div><span class="health-number">{{ dash.signups_30d }}</span><span>New customers</span></div></div>
              <RouterLink v-if="can('inventory.read')" :to="`/b/${route.params.bid}/inventory?low=1`" class="panel-link">{{ dash.stock.out }} variants out of stock <span>View inventory →</span></RouterLink>
            </section>
          </div>
          <section class="analytics-panel bestsellers"><div class="panel-heading"><div><h2>Best-selling products</h2><p>Your top performers by units sold</p></div><RouterLink v-if="can('catalog.read')" :to="`/b/${route.params.bid}/products`">View catalog →</RouterLink></div>
            <div v-if="dash.top_products.length" class="analytics-table"><table><thead><tr><th>Product</th><th>Units sold</th><th>Revenue</th><th>Unit sales</th></tr></thead><tbody><tr v-for="(p, i) in dash.top_products" :key="p.name"><td><span class="product-rank">{{ String(i + 1).padStart(2, '0') }}</span><strong>{{ p.name }}</strong></td><td>{{ Number(p.units).toLocaleString('en-IN') }}</td><td>{{ money(p.revenue) }}</td><td><span class="rank-track"><i :style="{ width: `${Number(p.units) / Math.max(1, Number(dash.top_products[0].units)) * 100}%` }"></i></span></td></tr></tbody></table></div><p v-else class="analytics-empty">Your best sellers will appear here after your first sale.</p>
          </section>
        </div>
        <aside v-if="can('insights.read')" class="recommendations-rail" aria-label="Recommendations">
          <section class="analytics-panel recommendations-panel"><div class="panel-heading"><div><span class="rail-eyebrow">YOUR NEXT BEST MOVES</span><h2>Recommendations <span class="count-badge">{{ recs.length }}</span></h2></div><AppIcon name="spark" :size="22" /></div>
            <p class="rail-description">Small actions. A healthier business.</p>
            <label class="rec-filter"><span class="sr-only">Filter recommendations</span><select v-model="kind" @change="expanded = false"><option value="">All recommendations</option><option value="REORDER">Restock</option><option value="DISCOUNT">Surplus</option><option value="DEAD_STOCK">Slow moving</option><option value="REVIEW">Review</option></select></label>
            <ErrorBox :error="recError" /><p v-if="!filtered.length" class="analytics-empty">You’re all caught up.</p>
            <article v-for="r in visibleRecs" :key="r.id" class="recommendation-item"><div class="rec-meta"><span :class="['rec-type', r.type.toLowerCase()]"><AppIcon :name="r.type === 'REORDER' ? 'inventory' : 'insights'" :size="13"/>{{ recLabels[r.type] }}</span><span :class="['rec-priority', r.severity]">{{ r.severity }} priority</span></div><h3>{{ r.title }}</h3><p>{{ r.action }}</p>
              <p class="rec-variant" v-if="r.variant"><strong>{{ options(r.variant.options) || r.variant.sku }}</strong><br /><span>{{ r.variant.sku }}</span></p>
              <a v-if="variantLink(r)" :href="variantLink(r)" target="_blank" rel="noopener">View this size/colour ↗</a>
              <details><summary>Why this recommendation?</summary><p>{{ r.summary }}</p><p v-for="w in r.evidence?.warnings || []" :key="w" class="warn">{{ w }}</p></details>
              <p v-if="r.type === 'DISCOUNT' && r.status === 'ACCEPTED' && !r.discount_application">Previously accepted. The discount has not been applied yet.</p>
              <button class="rec-explain" type="button" @click="ask(`Explain this recommendation and its risks: ${r.title}`)"><AppIcon name="spark" :size="13"/>Discuss with Guide</button>
              <div v-if="can('insights.act')" class="rec-actions">
                <button v-if="hasDiscount(r)" :disabled="busy === r.id || !can('catalog.write')" :title="!can('catalog.write') ? 'Catalog editing permission required' : ''" @click="discountPreview = r.id">Apply {{ r.suggested_discount_pct }}% off</button>
                <button v-else-if="r.type !== 'DISCOUNT'" :disabled="busy === r.id" @click="decide(r, 'accept')">Accept</button>
                <button :disabled="busy === r.id" @click="decide(r, 'postpone')">Snooze 7d</button><button :disabled="busy === r.id" aria-label="Dismiss recommendation" @click="decide(r, 'dismiss')"><AppIcon name="close" :size="14" /></button>
              </div>
              <div v-if="discountPreview === r.id && hasDiscount(r)" class="discount-confirmation" role="group" aria-label="Confirm discount">
                <strong>{{ options(r.variant?.options) || r.evidence.sku }}: {{ money(r.evidence.unit_price) }} → {{ money(discountedPrice(r)) }}</strong>
                <p>Only this size/colour will change in the shop and checkout. Other sizes and colours keep their prices. This price stays until edited; existing orders keep their prices.</p>
                <button :disabled="busy === r.id" @click="decide(r, 'accept')">{{ busy === r.id ? 'Applying…' : 'Confirm discount' }}</button>
                <button :disabled="busy === r.id" @click="discountPreview = null">Cancel</button>
              </div>
            </article>
            <button v-if="filtered.length > 4" class="rec-show-more" @click="expanded = !expanded">{{ expanded ? 'Show fewer' : `Show up to 20 (${filtered.length} total)` }}</button>
            <p v-if="appliedMessage" role="status" class="discount-success">{{ appliedMessage }}</p>
            <a v-if="appliedLink" :href="appliedLink" target="_blank" rel="noopener">View discounted size/colour ↗</a>
            <p class="rec-disclaimer">Discounts apply only after confirmation. Other recommendations record your decision without changing stock or prices.</p>
          </section>
          <section v-if="can('agent.use')" class="guide-promo"><span class="guide-promo-icon"><AppIcon name="assistant" :size="23"/></span><h3>Go beyond the numbers</h3><p>Ask why stock is building up, what to reorder, or how a discount affects margin.</p><button @click="ask('Analyse my inventory: compare stock cover, incoming orders and demand. What should I prioritise, and why?')">Ask Business Guide <span>↗</span></button></section>
        </aside>
      </div>
    </template>
  </main>
</template>

<style scoped>
.analytics-dashboard{--analytics-purple:#8650db;--analytics-muted:#9290a4;color:#252237;max-width:1600px;margin:0 auto;font-size:14px}
.analytics-heading{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:26px}.analytics-eyebrow{font-size:10px;letter-spacing:.15em;font-weight:750;color:#9891a5;margin:0 0 7px}.analytics-heading h1{font-size:clamp(23px,2.2vw,32px);letter-spacing:-1px;margin:0;display:flex;gap:12px;align-items:center}.analytics-heading p:not(.analytics-eyebrow){color:#8d879c;margin:8px 0 0;font-size:13px}.live-dot{height:7px;width:7px;border-radius:50%;background:#35b692;box-shadow:0 0 0 4px #e6f6ee}.analytics-toolbar{display:flex;align-items:center;gap:9px}.period-pill{display:flex;align-items:center;gap:8px;background:#fff;border:1px solid #e8e4f0;border-radius:9px;padding:11px 13px;font-size:11px;font-weight:650;white-space:nowrap}.analytics-refresh{display:grid;place-items:center;background:#fff;color:#756e87;border:1px solid #e8e4f0;padding:10px;border-radius:9px}.analytics-period{display:flex;align-items:center;gap:13px;color:#9690a5;font-size:11px;margin-bottom:18px}.analytics-period-active{background:#eee6fc;border-radius:7px;padding:7px 10px;color:#8251cc;font-weight:700}.analytics-period small{margin-left:auto;font-size:10px}
.analytics-kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;margin-bottom:22px}.metric-card{min-width:0;background:#fff;border:1px solid #eeebf3;border-radius:13px;padding:18px 19px;box-shadow:0 3px 10px #27204702}.metric-label{display:flex;justify-content:space-between;gap:5px;align-items:center;color:#777185;font-size:12px;font-weight:550}.metric-icon{width:31px;height:31px;background:#f5efff;color:#9a65df;border-radius:9px;display:grid;place-items:center;flex-shrink:0}.metric-card>strong{display:block;font-size:clamp(23px,2.2vw,34px);font-variant-numeric:tabular-nums;letter-spacing:-1px;margin:10px 0 13px;line-height:1.1;overflow-wrap:anywhere}.metric-footer{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.metric-footer small,.metric-baseline{font-size:10px;color:#aaa4b5}.metric-change{font-size:10px;font-weight:700;padding:3px 6px;border-radius:4px}.positive{background:#e6f7ee;color:#2a9c79}.negative{background:#fff0ee;color:#d86c68}
.analytics-layout{display:grid;grid-template-columns:minmax(0,1fr) 304px;gap:20px;align-items:start}.analytics-main{display:grid;gap:20px;min-width:0}.analytics-panel{background:#fff;border:1px solid #ebe7f1;border-radius:13px;padding:21px;min-width:0;box-shadow:0 3px 12px #20103802}.panel-heading{display:flex;justify-content:space-between;align-items:center;gap:12px}.panel-heading h2{font-size:14px;letter-spacing:-.2px;margin:0;font-weight:700}.panel-heading p{color:#a09aae;font-size:11px;margin:5px 0 0}.panel-heading>.app-icon{color:#b3a5c9}.panel-heading>a{font-size:11px;color:#9b64db;text-decoration:none;white-space:nowrap}.chart-legend{display:flex;gap:6px;align-items:center;font-size:10px;color:#9e96ab}.chart-legend i{height:7px;width:7px;background:#9461e8;border-radius:50%}.revenue-summary{display:flex;align-items:baseline;gap:12px;margin-top:20px;min-height:34px;flex-wrap:wrap}.revenue-summary>strong{font-size:24px;letter-spacing:-.7px;font-variant-numeric:tabular-nums}.revenue-summary>span{font-size:10px;color:#aaa1b7}.revenue-chart{margin:10px -5px -5px;min-width:0}.revenue-chart svg{width:100%;display:block;max-height:270px;overflow:visible}.revenue-chart text{font-size:10px;fill:#aaa1b6}.analytics-secondary{display:grid;grid-template-columns:1.06fr 1fr;gap:20px}.category-content{display:flex;align-items:center;justify-content:space-around;gap:18px;margin-top:24px;min-height:142px}.category-ring{width:132px;height:132px;border-radius:50%;display:grid;place-items:center;flex-shrink:0;transform:rotate(-90deg)}.category-ring>div{background:#fff;width:104px;height:104px;border-radius:50%;display:flex;flex-direction:column;align-items:center;justify-content:center;transform:rotate(90deg)}.category-ring strong{font-size:27px;font-weight:750;line-height:1.2}.category-ring span{font-size:10px;color:#aaa1b6}.category-legend{list-style:none;margin:0;padding:0;display:grid;gap:12px;min-width:0;flex:1}.category-legend li{display:flex;gap:7px;align-items:center;font-size:10px;color:#9790a4;min-width:0}.category-legend i{width:6px;height:6px;border-radius:50%;flex-shrink:0}.category-legend li>span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.category-legend li>strong{margin-left:auto;white-space:nowrap;color:#5b536b;font-size:10px;font-weight:650}.health-stat{margin-top:21px;display:grid;gap:4px}.health-stat>span{font-size:11px;color:#9a91a6}.health-stat>strong{font-size:27px;letter-spacing:-.7px}.health-stat>small{font-size:10px;color:#aaa2b4}.health-pair{display:grid;grid-template-columns:1fr 1fr;margin:16px 0 12px;gap:10px}.health-pair>div{display:flex;flex-direction:column;gap:3px}.health-pair>div+div{border-left:1px solid #f0ecf5;padding-left:18px}.health-number{font-size:22px;font-weight:700;color:#8270c0;line-height:1.2}.health-number.warning{color:#d69a4e}.health-pair span:last-child{font-size:10px;color:#aaa1b5}.panel-link{font-size:10px;color:#9b93a9;text-decoration:none;display:flex;justify-content:space-between;border-top:1px solid #f1edf6;padding-top:12px;gap:8px}.panel-link span{color:#9666d4}
.analytics-table{margin:18px -21px -14px;overflow:auto}.analytics-dashboard .analytics-table table{display:table;width:100%;border:none;margin:0;border-radius:0;font-size:11px;white-space:nowrap}.analytics-table th{background:#faf9fd;color:#a09aae;border-bottom:1px solid #f2eef6;padding:10px 18px;font-size:10px;font-weight:600}.analytics-table td{padding:14px 18px;border-color:#f4f1f7;color:#7f758f;vertical-align:middle}.analytics-table td:first-child{display:flex;align-items:center;gap:10px}.analytics-table td strong{font-weight:550;color:#53485f}.analytics-table th:last-child,.analytics-table td:last-child{min-width:95px}.product-rank{color:#bab0c9;font-size:10px;font-variant-numeric:tabular-nums}.rank-track{display:block;width:75px;background:#f0eafa;height:5px;border-radius:10px;overflow:hidden}.rank-track i{display:block;height:100%;background:#b295e5;border-radius:10px}
.recommendations-rail{display:grid;gap:18px;min-width:0}.recommendations-panel{padding:19px 16px}.rail-eyebrow{font-size:8px;color:#aaa0b8;font-weight:700;letter-spacing:.12em;display:block;margin-bottom:6px}.recommendations-panel .panel-heading h2{font-size:13px;display:flex;align-items:center;gap:6px}.recommendations-panel .panel-heading>.app-icon{color:#9d64df}.count-badge{font-size:9px;background:#f1eafb;padding:2px 6px;border-radius:5px;color:#9468c7}.rail-description{font-size:10px;color:#a49aaf;margin:8px 0 15px}.rec-filter select{width:100%;border:1px solid #ede7f2;background:#faf8fd;border-radius:7px;font-size:10px;padding:8px;color:#8c809f}.recommendation-item{padding:17px 0;border-bottom:1px solid #f0ebf5}.rec-meta{display:flex;align-items:center;justify-content:space-between;gap:5px}.rec-type{display:inline-flex;gap:4px;align-items:center;border-radius:4px;background:#f0ebfc;color:#9770c9;padding:3px 5px;font-size:9px;font-weight:650}.rec-type.reorder{background:#fff3e4;color:#ce9344}.rec-type.dead_stock{background:#fff0f2;color:#d87c92}.rec-priority{font-size:8px;color:#a99db7;text-transform:capitalize}.rec-priority.high,.rec-priority.critical{color:#dc8e64}.recommendation-item h3{font-size:11px;line-height:1.5;font-weight:650;margin:9px 0 5px;color:#62546e}.recommendation-item>p,.recommendation-item details p{font-size:10px;line-height:1.65;color:#a295ae;margin:0}.recommendation-item details{margin-top:8px}.recommendation-item summary{cursor:pointer;font-size:9px;color:#9b80ba}.recommendation-item details p{padding-top:6px}.rec-explain{background:none;color:#9d73ce;padding:0;font-size:9px;display:flex;gap:4px;align-items:center;margin:9px 0}.rec-actions{display:flex;gap:5px;margin-top:10px}.rec-actions button{background:#f4f0fa;color:#9b81b9;border:1px solid #e9dff5;font-size:9px;padding:5px 8px;border-radius:5px}.rec-actions button:first-child{background:#9669ce;color:#fff;border-color:#9669ce}.rec-actions button:last-child{margin-left:auto;display:grid;place-items:center;background:none;color:#c2b6ce;border-color:transparent}.rec-disclaimer{font-size:9px;line-height:1.6;color:#b0a6ba;margin:14px 0 0}.rec-show-more{width:100%;background:#faf7fe;color:#a787cc;font-size:10px;margin-top:12px;padding:8px}.guide-promo{border:1px solid #e4d9f4;border-radius:13px;padding:20px;background:linear-gradient(135deg,#eee6fb,#f8f5ff);position:relative;overflow:hidden}.guide-promo-icon{display:grid;place-items:center;height:36px;width:36px;border-radius:12px;background:#fff9;box-shadow:0 3px 10px #8851b80b;color:#a47cd5}.guide-promo h3{font-size:14px;margin:12px 0 7px;color:#76608e}.guide-promo p{font-size:11px;line-height:1.65;color:#a18eaf;margin:0 0 16px}.guide-promo button{display:flex;justify-content:space-between;align-items:center;width:100%;font-size:11px;background:#9564cd;padding:10px 12px;border-radius:7px}.analytics-empty,.analytics-loading{padding:25px 10px;text-align:center;color:#a095af;font-size:12px}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
@media(min-width:1500px){.analytics-layout{grid-template-columns:minmax(0,1fr) 320px}.analytics-secondary{grid-template-columns:1fr 1fr}}
@media(max-width:1150px){.analytics-layout{grid-template-columns:minmax(0,1fr) 270px;gap:16px}.analytics-secondary{grid-template-columns:1fr}.category-content{justify-content:flex-start}.category-legend{max-width:220px}.metric-card{padding:15px}.metric-label{font-size:10px}.metric-footer small{display:none}}
@media(max-width:900px){.analytics-layout{grid-template-columns:1fr}.recommendations-rail{grid-template-columns:1.3fr 1fr}.analytics-secondary{grid-template-columns:1fr 1fr}.analytics-heading{align-items:flex-start}.analytics-period small{display:none}.analytics-heading p:not(.analytics-eyebrow){max-width:260px}.category-content{flex-wrap:wrap}.category-legend{min-width:100px}.metric-card>strong{font-size:25px}}
@media(max-width:600px){.analytics-heading{flex-direction:column;gap:15px;margin-bottom:18px}.analytics-heading p:not(.analytics-eyebrow){max-width:none}.analytics-toolbar{width:100%}.period-pill{flex:1;justify-content:center}.analytics-kpis{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-bottom:15px}.metric-card{padding:14px}.metric-card>strong{font-size:27px}.metric-icon{width:26px;height:26px}.metric-label{font-size:10px}.metric-change{font-size:9px}.analytics-period{font-size:9px;gap:8px}.analytics-period-active{white-space:nowrap}.analytics-secondary,.recommendations-rail{grid-template-columns:1fr}.analytics-main,.analytics-layout{gap:15px}.analytics-panel{padding:17px}.analytics-table{margin-inline:-17px}.revenue-summary>strong{font-size:21px}.revenue-summary{gap:5px}.category-content{flex-wrap:nowrap;justify-content:space-around}.recommendations-panel{padding:19px}.recommendation-item h3{font-size:13px}.recommendation-item>p,.recommendation-item details p{font-size:12px}.rec-meta,.rec-actions{gap:8px}.rec-actions button,.rec-explain,.recommendation-item summary{font-size:11px}.rec-type,.rec-priority{font-size:10px}.rec-disclaimer{font-size:10px}.category-legend{max-width:none}.guide-promo h3{font-size:16px}.analytics-table td,.analytics-table th{padding-inline:12px}}
/* Keep compact cards legible on the light analytics canvas. */
.analytics-heading p:not(.analytics-eyebrow),.panel-heading p,.metric-label,.analytics-period{color:#797186}.recommendation-item>p,.recommendation-item details p{font-size:11px;color:#81738e}.recommendation-item h3{font-size:12px}.rec-disclaimer{color:#8c8098}.rec-explain,.recommendation-item summary{font-size:10px}.category-legend li,.health-stat>span,.health-pair span:last-child{color:#82758f}.metric-footer small{color:#8d8199}.guide-promo p{color:#847095}
</style>

<style scoped>
.discount-confirmation{margin-top:12px;padding:12px;border:1px solid #d9c6ef;border-radius:8px;background:#faf7ff;font-size:12px;line-height:1.6;color:#62546e}.discount-confirmation strong{display:block;overflow-wrap:anywhere}.discount-confirmation p{margin:6px 0 10px}.discount-confirmation button{font-size:11px;margin:0 6px 4px 0}.discount-success{font-size:12px;color:#16704a;line-height:1.6;padding:10px;background:#edf8f1;border-radius:8px;overflow-wrap:anywhere}
</style>

<style scoped>
.recommendation-item .rec-variant{margin:8px 0;font-size:11px;line-height:1.6;overflow-wrap:anywhere;color:#62546e}.recommendation-item>a,.recommendations-panel>a{display:inline-block;font-size:11px;margin-top:8px;color:#8655b6}
</style>
