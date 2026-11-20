<script setup>
import { ref } from 'vue'
import { useRoute } from 'vue-router'
import { adminApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import StatusBadge from '../shared/StatusBadge.vue'
import { date, day, money } from '../shared/format.js'
import { base, can } from './store.js'

const route = useRoute()
const b = base(route.params.bid)
const items = ref([])
const suppliers = ref([])
const pos = ref([])
const error = ref(null)
const note = ref('')
const busy = ref(false)
const open = ref(null) // the invoice being edited
const file = ref(null)
const load = () => adminApi.get(b + '/purchase-invoices').then((r) => (items.value = r.items)).catch((e) => (error.value = e))
load()
adminApi.get(b + '/suppliers').then((r) => (suppliers.value = r.items)).catch(() => {})
adminApi.get(b + '/purchase-orders', { page_size: 100 }).then((r) => (pos.value = r.items)).catch(() => {})

const toBase64 = (f) => new Promise((resolve, reject) => {
  const reader = new FileReader()
  reader.onload = () => resolve(String(reader.result).split(',')[1])
  reader.onerror = reject
  reader.readAsDataURL(f)
})

async function run(fn, done) {
  busy.value = true
  error.value = null
  note.value = ''
  try {
    const r = await fn()
    await load()
    note.value = done
    return r
  } catch (e) {
    error.value = e
  } finally {
    busy.value = false
  }
}

async function upload() {
  const f = file.value?.files?.[0]
  if (!f) return (error.value = { message: 'Choose the supplier’s invoice (PDF, PNG or JPEG).' })
  if (f.size > 10 * 1024 * 1024) return (error.value = { message: 'Files can be at most 10 MB.' })
  const pi = await run(async () => {
    const doc = await adminApi.post(b + '/documents', { filename: f.name, content_base64: await toBase64(f) })
    return adminApi.post(b + '/purchase-invoices', { document_id: doc.id })
  }, 'Uploaded. Fill in the details from the document.')
  if (pi) edit(pi)
}

function edit(pi) {
  open.value = { id: pi.id, status: pi.status, document_id: pi.document_id, supplier_id: pi.supplier_id || '', supplier_invoice_number: pi.supplier_invoice_number || '',
    invoice_date: pi.invoice_date || '', due_date: pi.due_date || '', taxable_total: pi.taxable_total, tax_total: pi.tax_total,
    grand_total: pi.grand_total, linked_po_id: pi.linked_po_id || '', problems: pi.problems, corrections: pi.corrections }
}
async function save() {
  const o = open.value
  const r = await run(() => adminApi.patch(`${b}/purchase-invoices/${o.id}`, {
    supplier_id: o.supplier_id || null, supplier_invoice_number: o.supplier_invoice_number || null, invoice_date: o.invoice_date || null,
    due_date: o.due_date || null, taxable_total: String(o.taxable_total || 0), tax_total: String(o.tax_total || 0),
    grand_total: String(o.grand_total || 0), linked_po_id: o.linked_po_id || null }), 'Saved.')
  if (r) edit(r)
}
async function step(action) {
  const r = await run(() => adminApi.post(`${b}/purchase-invoices/${open.value.id}/${action}`), `Done: ${action}.`)
  if (r) edit(r)
}
async function download(pi) {
  const r = await adminApi.raw(`${b}/documents/${pi.document_id}/content`)
  const url = URL.createObjectURL(await r.blob())
  window.open(url, '_blank', 'noopener')
  setTimeout(() => URL.revokeObjectURL(url), 60000)
}
</script>

<template>
  <h1>Supplier invoices</h1>
  <p class="muted small">Upload the bill you received, enter its numbers, then approve it. Only approved bills become payments due. The original file is kept unchanged.</p>
  <ErrorBox :error="error" :labels="{ supplier_id: 'Supplier', supplier_invoice_number: 'Invoice number', invoice_date: 'Invoice date', grand_total: 'Total', due_date: 'Due date', linked_po_id: 'Purchase order' }" />
  <p v-if="note" class="ok">{{ note }}</p>
  <div v-if="can('purchasing.write')" class="card row">
    <input ref="file" type="file" accept="application/pdf,image/png,image/jpeg" aria-label="Supplier invoice file" />
    <button :disabled="busy" @click="upload">Upload bill</button>
  </div>
  <form v-if="open" class="card stack" @submit.prevent="save">
    <div class="row between"><h3>Bill <StatusBadge :status="open.status" /></h3><button type="button" class="ghost" @click="download(open)">Open original</button></div>
    <fieldset :disabled="!['DRAFT', 'NEEDS_REVIEW'].includes(open.status) || !can('purchasing.write')" class="grid3">
      <label>Supplier <select v-model="open.supplier_id"><option value="">—</option><option v-for="s in suppliers" :key="s.id" :value="s.id">{{ s.name }}</option></select></label>
      <label>Their invoice number <input v-model="open.supplier_invoice_number" maxlength="60" /></label>
      <label>Purchase order <select v-model="open.linked_po_id"><option value="">—</option><option v-for="p in pos.filter((x) => !open.supplier_id || x.supplier_id === open.supplier_id)" :key="p.id" :value="p.id">{{ p.number }}</option></select></label>
      <label>Invoice date <input v-model="open.invoice_date" type="date" /></label>
      <label>Due date (blank = payment terms) <input v-model="open.due_date" type="date" /></label>
      <span></span>
      <label>Taxable ₹ <input v-model="open.taxable_total" type="number" min="0" step="0.01" /></label>
      <label>Tax ₹ <input v-model="open.tax_total" type="number" min="0" step="0.01" /></label>
      <label>Total ₹ <input v-model="open.grand_total" type="number" min="0" step="0.01" /></label>
    </fieldset>
    <ul v-if="Object.keys(open.problems || {}).length" class="warn small"><li v-for="(m, k) in open.problems" :key="k">{{ m }}</li></ul>
    <div class="row">
      <button v-if="['DRAFT', 'NEEDS_REVIEW'].includes(open.status) && can('purchasing.write')" :disabled="busy">Save</button>
      <button v-if="open.status === 'DRAFT' && can('purchasing.write')" type="button" class="ghost" :disabled="busy" @click="step('submit')">Send for review</button>
      <button v-if="['DRAFT', 'NEEDS_REVIEW'].includes(open.status) && can('purchasing.approve')" type="button" :disabled="busy" @click="step('approve')">Approve (creates payment due)</button>
      <button v-if="['DRAFT', 'NEEDS_REVIEW'].includes(open.status) && can('purchasing.approve')" type="button" class="danger" :disabled="busy" @click="step('reject')">Reject</button>
      <button type="button" class="ghost" @click="open = null">Close</button>
    </div>
    <details v-if="open.corrections?.length"><summary>{{ open.corrections.length }} correction(s)</summary>
      <p v-for="(c, n) in open.corrections" :key="n" class="small">{{ date(c.at) }}: {{ Object.entries(c.changes).map(([k, v]) => `${k} ${v.from ?? '—'} → ${v.to ?? '—'}`).join('; ') }}</p></details>
  </form>
  <table v-if="items.length">
    <thead><tr><th>Supplier</th><th>Their number</th><th>Date</th><th>Status</th><th class="num">Total</th><th></th></tr></thead>
    <tbody><tr v-for="pi in items" :key="pi.id"><td>{{ pi.supplier || '—' }}</td><td>{{ pi.supplier_invoice_number || '—' }}</td><td>{{ day(pi.invoice_date) }}</td>
      <td><StatusBadge :status="pi.status" /></td><td class="num">{{ money(pi.grand_total) }}</td>
      <td><button class="ghost small" @click="edit(pi)">Open</button></td></tr></tbody>
  </table>
  <p v-else class="empty">No supplier bills yet.</p>
</template>
