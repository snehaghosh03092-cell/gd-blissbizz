import assert from 'node:assert/strict'
import test from 'node:test'
import { initialVariant } from '../src/shop/variants.js'

const variants = [
  { id: 'black-28', options: { size: '28', color: 'Black' }, in_stock: true, price: '1799.00' },
  { id: 'indigo-36', options: { size: '36', color: 'Indigo' }, in_stock: true, price: '1259.30' },
]

test('discount link opens the discounted size/colour rather than the first option', () => {
  assert.equal(initialVariant(variants, 'indigo-36').price, '1259.30')
  assert.deepEqual(initialVariant(variants, 'indigo-36').options, { size: '36', color: 'Indigo' })
  assert.equal(initialVariant(variants).id, 'black-28')
})

test('invalid and cross-shop option IDs do not select an unavailable foreign variant', () => {
  assert.equal(initialVariant(variants, 'foreign-variant').id, 'black-28')
  assert.equal(initialVariant([], 'indigo-36'), null)
})

test('an exact requested option stays selected even if it has sold out', () => {
  const items = variants.map((v) => v.id === 'indigo-36' ? { ...v, in_stock: false } : v)
  assert.equal(initialVariant(items, 'indigo-36').id, 'indigo-36')
  assert.equal(initialVariant(items).id, 'black-28')
})
