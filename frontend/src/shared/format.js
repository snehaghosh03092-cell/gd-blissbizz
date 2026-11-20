const inr = new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 2 })

export const money = (v) => (v === null || v === undefined || v === '' ? '—' : inr.format(Number(v)))
export const qty = (v) => (v === null || v === undefined ? '—' : String(Number(v)))
export const date = (iso) => (iso ? new Date(iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z').toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' }) : '—')
export const day = (iso) => (iso ? new Date(iso).toLocaleDateString('en-IN', { dateStyle: 'medium' }) : '—')
export const words = (s) => (s ? s.toLowerCase().replaceAll('_', ' ') : '')
export const options = (o) => Object.entries(o || {}).map(([k, v]) => `${k}: ${v}`).join(', ')

// A new random key for each user action that must not happen twice (checkout, receipts...).
export const newKey = (prefix) => `${prefix}-${crypto.randomUUID()}`

export const STATUS_TONE = {
  CONFIRMED: 'info', PENDING_PAYMENT: 'warn', PACKED: 'info', SHIPPED: 'info', DELIVERED: 'good',
  CANCELLED: 'bad', RETURN_REQUESTED: 'warn', RETURNED: 'muted', PAID: 'good', UNPAID: 'warn',
  REFUNDED: 'muted', PARTIALLY_REFUNDED: 'warn', FAILED: 'bad', DRAFT: 'muted', PUBLISHED: 'good',
  ARCHIVED: 'muted', APPROVED: 'good', NEEDS_REVIEW: 'warn', REJECTED: 'bad', RECEIVED: 'good',
  PARTIALLY_RECEIVED: 'warn', REQUESTED: 'warn', DUE: 'warn', PARTIAL: 'warn', IN_TRANSIT: 'info',
  OK: 'good', LOW: 'warn', OUT: 'bad', NOT_FOR_SALE: 'muted', IN_STOCK: 'good', LOW_STOCK: 'warn',
  OUT_OF_STOCK: 'bad', POSTED: 'good', ISSUED: 'good',
}
