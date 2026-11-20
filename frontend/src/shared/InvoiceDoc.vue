<script setup>
// Printable GST invoice / bill of supply with its credit notes. "Print" -> save as PDF in the browser.
import { date, money } from './format.js'
defineProps({ invoice: { type: Object, required: true } })
</script>

<template>
  <article class="invoice">
    <header class="row between">
      <div>
        <h2>{{ invoice.doc_type === 'TAX_INVOICE' ? 'Tax invoice' : 'Bill of supply' }}</h2>
        <p class="muted">No. <strong>{{ invoice.number }}</strong> · {{ date(invoice.issued_at) }}</p>
      </div>
      <button class="ghost no-print" @click="() => window.print()">Print / save PDF</button>
    </header>
    <div class="grid2">
      <section>
        <h4>Seller</h4>
        <p><strong>{{ invoice.seller.legal_name || invoice.seller.name }}</strong><br />
          <span v-if="invoice.seller.address">{{ invoice.seller.address }}<br /></span>
          <span v-if="invoice.seller.gstin">GSTIN {{ invoice.seller.gstin }}<br /></span>
          State code {{ invoice.seller.state_code || '—' }}</p>
      </section>
      <section>
        <h4>Bill to</h4>
        <p><strong>{{ invoice.buyer.name }}</strong><br />
          <span v-if="invoice.buyer.address">{{ invoice.buyer.address }}<br /></span>
          Place of supply: {{ invoice.place_of_supply_state || '—' }}
          ({{ invoice.is_inter_state ? 'inter-state, IGST' : 'within state, CGST + SGST' }})</p>
      </section>
    </div>
    <table>
      <thead>
        <tr><th>#</th><th>Item</th><th>HSN/SAC</th><th class="num">Qty</th><th class="num">Rate</th>
          <th class="num">Taxable</th><th class="num">CGST</th><th class="num">SGST</th><th class="num">IGST</th><th class="num">Total</th></tr>
      </thead>
      <tbody>
        <tr v-for="l in invoice.lines" :key="l.position">
          <td>{{ l.position }}</td><td>{{ l.description }}</td><td>{{ l.hsn_sac || '—' }}</td>
          <td class="num">{{ l.quantity }} {{ l.unit }}</td><td class="num">{{ money(l.unit_price) }}</td>
          <td class="num">{{ money(l.taxable_value) }}</td>
          <td class="num">{{ money(l.cgst) }}<small v-if="Number(l.cgst)"> @{{ l.cgst_rate }}%</small></td>
          <td class="num">{{ money(l.sgst) }}<small v-if="Number(l.sgst)"> @{{ l.sgst_rate }}%</small></td>
          <td class="num">{{ money(l.igst) }}<small v-if="Number(l.igst)"> @{{ l.igst_rate }}%</small></td>
          <td class="num">{{ money(l.line_total) }}</td>
        </tr>
      </tbody>
      <tfoot>
        <tr><td colspan="5">Totals</td><td class="num">{{ money(invoice.taxable_total) }}</td><td class="num">{{ money(invoice.cgst_total) }}</td>
          <td class="num">{{ money(invoice.sgst_total) }}</td><td class="num">{{ money(invoice.igst_total) }}</td><td></td></tr>
        <tr><td colspan="9">Round off</td><td class="num">{{ money(invoice.round_off) }}</td></tr>
        <tr class="grand"><td colspan="9">Amount {{ invoice.prices_include_tax ? '(prices include GST)' : '' }}</td><td class="num">{{ money(invoice.grand_total) }}</td></tr>
      </tfoot>
    </table>
    <section v-if="invoice.credit_notes?.length">
      <h4>Credit notes</h4>
      <table>
        <thead><tr><th>Number</th><th>Date</th><th>Reason</th><th class="num">Taxable</th><th class="num">Amount</th></tr></thead>
        <tbody>
          <tr v-for="n in invoice.credit_notes" :key="n.id">
            <td>{{ n.number }}</td><td>{{ date(n.issued_at) }}</td><td>{{ n.reason }}</td>
            <td class="num">{{ money(n.taxable_total) }}</td><td class="num">{{ money(n.grand_total) }}</td>
          </tr>
        </tbody>
      </table>
    </section>
    <p class="muted small">Computer-generated document. Fingerprint {{ invoice.content_hash?.slice(0, 16) }}…</p>
  </article>
</template>
