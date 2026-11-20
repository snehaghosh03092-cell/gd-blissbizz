<script setup>
import { storefrontUrl } from '../shared/hosts.js'
// Guided setup: details -> GST -> stock location -> look -> policies -> go live. Each step saves on its
// own; the server decides what is still missing (and never asks again for what is answered).
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { adminApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import StatusBadge from '../shared/StatusBadge.vue'
import AppIcon from '../shared/AppIcon.vue'
import StorefrontHero from '../shared/StorefrontHero.vue'
import { storefrontDesign, storefrontTemplates } from '../shared/storefrontTemplates.js'
import { base, can, enterBusiness, session } from './store.js'

const route = useRoute()
const b = base(route.params.bid)
const ob = ref(null)
const store = ref(null)
const warehouses = ref([])
const f = ref({})
const look = ref({ template_key: 'fashion', primary: '#1f2937', accent: '#f472b6', logo_text: '', hero_title: '', hero_subtitle: '' })
const pol = ref({ shipping_days_min: 3, shipping_days_max: 7, free_shipping_over: '999', flat_shipping_fee: '49', return_window_days: 7,
  return_conditions: 'Unused items with tags.', refund_method: 'Original payment method or bank transfer', cancel_window_hours: 24,
  support_email: '', support_phone: '', show_exact_stock: false })
const wh = ref({ code: 'MAIN', name: 'Main store', state_code: '' })
const gst = ref(null)
const error = ref(null)
const note = ref('')
const busy = ref(false)
const liveHost = ref('')
const previewDesign = computed(() => storefrontDesign(look.value.template_key, look.value))
const published = computed(() => store.value?.storefront?.status === 'PUBLISHED')

async function load() {
  ob.value = await adminApi.get(`${b}/onboarding`)
  f.value = { ...ob.value.fields }
  wh.value.state_code = f.value.state_code || ''
  store.value = await adminApi.get(`${b}/storefront/settings`)
  warehouses.value = (await adminApi.get(`${b}/warehouses`)).items
  const sf = store.value.storefront
  if (sf) look.value = { template_key: sf.template_key, primary: sf.theme.primary, accent: sf.theme.accent, logo_text: sf.theme.logo_text || '',
    hero_title: sf.draft_config.hero?.title || '', hero_subtitle: sf.draft_config.hero?.subtitle || '' }
  else look.value.hero_title = `Welcome to ${f.value.name}`
  const p = store.value.policies
  if (p.shipping_days_min !== null) pol.value = Object.fromEntries(Object.entries(pol.value).map(([k, v]) => [k, p[k] ?? v]))
  if (!pol.value.support_email) pol.value.support_email = f.value.contact_email || ''
}
load().catch((e) => (error.value = e))

const steps = computed(() => [
  ['Details', ob.value?.complete && ob.value?.state !== 'DRAFT'],
  ['Stock location', warehouses.value.length > 0],
  ['Look', !!store.value?.storefront],
  ['Policies', store.value?.policies?.shipping_days_min !== null && store.value?.policies?.shipping_days_min !== undefined],
  ['Live', store.value?.storefront?.status === 'PUBLISHED'],
])
const completedSteps = computed(() => steps.value.filter(([, done]) => done).length)
const completion = computed(() => Math.round((completedSteps.value / steps.value.length) * 100))

async function run(fn, done) {
  busy.value = true
  error.value = null
  note.value = ''
  try {
    await fn()
    await load()
    note.value = done
  } catch (e) {
    error.value = e
  } finally {
    busy.value = false
  }
}

const blank = (v) => (v === '' ? null : v)
function saveDetails() {
  const keys = ['name', 'legal_name', 'description', 'category', 'constitution', 'sales_mode', 'state_code', 'city', 'postal_code', 'address', 'contact_email', 'contact_phone', 'prices_include_tax']
  const body = Object.fromEntries(keys.filter((k) => f.value[k] !== ob.value.fields[k]).map((k) => [k, blank(f.value[k])]))
  if (f.value.gst_registered !== ob.value.fields.gst_registered || f.value.gstin !== ob.value.fields.gstin) {
    body.gst_registered = !!f.value.gst_registered
    body.gstin = f.value.gst_registered ? blank(f.value.gstin) : null
  }
  run(async () => {
    if (Object.keys(body).length) await adminApi.patch(`${b}/onboarding`, body)
    if (ob.value.state !== 'ACTIVE' && ob.value.state !== 'READY_FOR_STORE_SETUP') await adminApi.post(`${b}/onboarding/submit`)
  }, 'Details saved.')
}
async function checkGstin() {
  gst.value = await adminApi.post('/tools/gstin-check', { gstin: f.value.gstin || '', state_code: f.value.state_code || null }).catch((e) => ({ ok: false, message: e.message }))
}
const addWarehouse = () => run(() => adminApi.post(`${b}/warehouses`, { ...wh.value, state_code: wh.value.state_code || null }), 'Stock location added.')
function pickTemplate(key) {
  if (look.value.template_key === key) return
  const [primary, accent] = storefrontTemplates[key].colors
  look.value.template_key = key
  look.value.primary = primary
  look.value.accent = accent
}
const saveLook = () => run(() => adminApi.put(`${b}/storefront/setup`, { template_key: look.value.template_key, hero_title: look.value.hero_title,
  hero_subtitle: look.value.hero_subtitle || null, theme: { primary: look.value.primary, accent: look.value.accent, logo_text: look.value.logo_text || null } }), 'Look saved.')
const savePolicies = () => run(() => adminApi.put(`${b}/policies`, Object.fromEntries(Object.entries(pol.value).map(([k, v]) => [k, blank(v)]))), 'Policies saved.')
const goLive = async () => {
  const firstPublish = !published.value
  await run(async () => {
  const r = await adminApi.post(`${b}/storefront/publish`)
  liveHost.value = r.shop_url_host
  await enterBusiness(route.params.bid, true) // refresh in place (status is now ACTIVE)
  }, firstPublish ? 'Your shop is live!' : 'Website changes published.')
  if (firstPublish && !error.value) session.guideOpen = true
}
const shopUrl = computed(() => storefrontUrl(f.value.slug, liveHost.value))
</script>

<template>
  <header class="setup-hero">
    <div>
      <h1>{{ published ? 'Update your website' : 'Create your website' }}</h1>
      <p>{{ published ? 'Refine your design, business details and shop policies, then publish the changes.' : 'Choose a storefront design, then finish the details and publish your shop.' }}</p>
    </div>
    <div class="setup-score" aria-label="Setup progress">
      <strong>{{ completion }}%</strong><span>{{ completedSteps }} of {{ steps.length }} steps</span>
    </div>
  </header>
  <div class="setup-progress" :style="{ '--progress': `${completion}%` }"><span></span></div>
  <ol class="steps setup-steps"><li v-for="([label, done], n) in steps" :key="label" :class="done ? 'done' : (steps.findIndex(([, d]) => !d) === n ? 'now' : '')">
    <span class="step-dot"><AppIcon v-if="done" name="check" :size="14" /><template v-else>{{ n + 1 }}</template></span>{{ label }}</li></ol>
  <ErrorBox :error="error" :labels="{ gstin: 'GSTIN', state_code: 'State', postal_code: 'PIN code', sales_mode: 'How you sell', category: 'What you sell', city: 'City' }" />
  <p v-if="note" class="ok">{{ note }}</p>
  <div v-if="ob" class="setup-workspace setup-workspace-single">
    <div class="setup-form">
    <section class="card setup-card" id="template-gallery">
      <h2>Choose a template</h2>
      <div class="template-grid" role="group" aria-label="Storefront templates">
        <button
          v-for="(template, key) in storefrontTemplates" :key="key" type="button" class="template-card"
          :value="key"
          :aria-pressed="look.template_key === key" :disabled="busy || !can('storefront.write')"
          @click="pickTemplate(key)"
        >
          <span class="template-preview" :class="storefrontDesign(key).classes" :style="storefrontDesign(key).colors" aria-hidden="true">
            <span class="template-browser"><i></i><i></i><i></i></span>
            <StorefrontHero :title="template.title" preview />
            <span class="template-catalog"><i></i><i></i><i></i></span>
          </span>
          <span class="template-label">
            <strong>{{ template.name }}</strong>
            <AppIcon v-if="look.template_key === key" name="check" :size="16" />
          </span>
          <span class="template-description">{{ template.description }}</span>
        </button>
      </div>
      <button v-if="can('storefront.write')" type="button" :disabled="busy || !look.hero_title" @click="saveLook">Save template</button>
    </section>
    <section class="card setup-card" id="business-details">
      <div class="section-heading"><span class="section-icon"><AppIcon name="building" /></span><div><p>STEP 1</p><h2>Your business</h2></div><StatusBadge v-if="session.current" :status="session.current.status" /></div>
      <p v-if="ob.next_questions.length" class="banner small"><strong>Still needed:</strong> {{ ob.next_questions.join(' · ') }}</p>
      <fieldset class="grid3" :disabled="!can('business.write')">
        <label>Business name <input v-model="f.name" maxlength="150" /></label>
        <label>Legal name (optional) <input v-model="f.legal_name" maxlength="200" /></label>
        <label>What you sell <select v-model="f.category"><option :value="null">Choose…</option><option v-for="c in ob.categories" :key="c" :value="c">{{ c }}</option></select></label>
        <label>State <select v-model="f.state_code"><option :value="null">Choose…</option><option v-for="(name, code) in ob.states" :key="code" :value="code">{{ name }}</option></select></label>
        <label>City <input v-model="f.city" maxlength="80" /></label>
        <label>PIN code <input v-model="f.postal_code" maxlength="6" pattern="[1-9][0-9]{5}" /></label>
        <label>How you sell <select v-model="f.sales_mode"><option :value="null">Choose…</option><option value="ONLINE">Online only</option><option value="OFFLINE">In a physical shop</option><option value="HYBRID">Both</option></select></label>
        <label>Contact email <input v-model="f.contact_email" type="email" /></label>
        <label>Contact phone <input v-model="f.contact_phone" maxlength="32" /></label>
      </fieldset>
      <h4>GST</h4>
      <fieldset class="stack gst-options" :disabled="!can('business.write')">
        <div class="gst-option-row">
          <label class="gst-option">
            <input v-model="f.gst_registered" type="checkbox" />
            <span>Business is GST-registered</span>
          </label>
          <label class="gst-option">
            <input v-model="f.prices_include_tax" type="checkbox" />
            <span>Listed prices include GST</span>
          </label>
        </div>
        <div v-if="f.gst_registered" class="row">
          <input v-model="f.gstin" maxlength="15" placeholder="15-character GSTIN" aria-label="GSTIN" @change="gst = null" />
          <button type="button" class="ghost" @click="checkGstin">Check GSTIN</button>
          <span v-if="gst" :class="gst.ok ? 'ok small' : 'bad small'">{{ gst.message }}</span>
        </div>
        <p class="muted small">We check the GSTIN's format, state and checksum. That is not a government verification. We never ask for PAN, Aadhaar or bank details.</p>
      </fieldset>
      <button v-if="can('business.write')" :disabled="busy" @click="saveDetails">Save details</button>
    </section>
    <section class="card setup-card">
      <div class="section-heading"><span class="section-icon"><AppIcon name="inventory" /></span><div><p>STEP 2</p><h2>Where you keep stock</h2></div></div>
      <p v-for="w in warehouses" :key="w.id">{{ w.code }} · {{ w.name }} <span v-if="w.is_default" class="muted">(default)</span></p>
      <form v-if="!warehouses.length" class="row" @submit.prevent="addWarehouse">
        <input v-model="wh.code" required pattern="[A-Za-z0-9_\-]{2,20}" aria-label="Code" />
        <input v-model="wh.name" required maxlength="100" aria-label="Name" />
        <button :disabled="busy">Add stock location</button>
      </form>
    </section>
    <section class="card setup-card">
      <div class="section-heading"><span class="section-icon"><AppIcon name="spark" /></span><div><p>STEP 3</p><h2>How your shop looks</h2></div></div>
      <div class="grid3">
        <label>Main colour <input v-model="look.primary" type="color" /></label>
        <label>Accent colour <input v-model="look.accent" type="color" /></label>
        <label>Logo text <input v-model="look.logo_text" maxlength="40" :placeholder="f.name" /></label>
        <label>Home page title <input v-model="look.hero_title" required maxlength="120" /></label>
        <label>Home page subtitle <input v-model="look.hero_subtitle" maxlength="200" /></label>
      </div>
      <div class="design-preview" :class="previewDesign.classes" :style="previewDesign.colors">
        <StorefrontHero :title="look.hero_title || 'Your title'" :subtitle="look.hero_subtitle" preview />
      </div>
      <button v-if="can('storefront.write')" :disabled="busy || !look.hero_title" @click="saveLook">Save look</button>
    </section>
    <section class="card setup-card">
      <div class="section-heading"><span class="section-icon"><AppIcon name="orders" /></span><div><p>STEP 4</p><h2>Shipping, returns and contact</h2></div></div>
      <div class="grid3">
        <label>Delivery from (days) <input v-model.number="pol.shipping_days_min" type="number" min="0" max="365" /></label>
        <label>Delivery up to (days) <input v-model.number="pol.shipping_days_max" type="number" min="0" max="365" /></label>
        <label>Free shipping from ₹ <input v-model="pol.free_shipping_over" type="number" min="0" /></label>
        <label>Otherwise shipping costs ₹ <input v-model="pol.flat_shipping_fee" type="number" min="0" /></label>
        <label>Returns accepted for (days, 0 = none) <input v-model.number="pol.return_window_days" type="number" min="0" max="365" /></label>
        <label>Cancel within (hours) <input v-model.number="pol.cancel_window_hours" type="number" min="0" max="720" /></label>
        <label>Return conditions <input v-model="pol.return_conditions" maxlength="1000" /></label>
        <label>Refunds go to <input v-model="pol.refund_method" maxlength="190" /></label>
        <label>Support email <input v-model="pol.support_email" type="email" /></label>
        <label>Support phone <input v-model="pol.support_phone" maxlength="40" /></label>
        <label class="check"><input v-model="pol.show_exact_stock" type="checkbox" /> Show exact stock counts to shoppers</label>
      </div>
      <button v-if="can('storefront.write')" :disabled="busy" @click="savePolicies">Save policies</button>
    </section>
    <section class="card setup-card launch-card">
      <div class="section-heading"><span class="section-icon"><AppIcon name="shop" /></span><div><p>STEP 5</p><h2>Open your doors</h2></div></div>
      <p class="muted">Publishing creates your shop site at its own address. After that, your full workspace opens so you can add products, stock and start taking orders.</p>
      <p v-if="store?.storefront?.status === 'PUBLISHED'" class="ok">Live at <a :href="shopUrl" target="_blank" rel="noopener">{{ shopUrl }}</a>
        <span v-if="store.storefront.unpublished_changes"> · you have changes that are not live yet</span></p>
      <button v-if="can('storefront.publish')" :disabled="busy" @click="goLive">{{ store?.storefront?.status === 'PUBLISHED' ? 'Publish changes' : 'Go live' }}</button>
      <p v-else class="muted small">Only the owner can publish the shop.</p>
    </section>
    </div>
  </div>
</template>
