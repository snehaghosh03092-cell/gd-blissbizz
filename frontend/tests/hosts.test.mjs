import assert from 'node:assert/strict'
import test from 'node:test'
import { appForLocation, storefrontUrl } from '../src/shared/hosts.js'

test('production hosts select the correct application', () => {
  for (const domain of ['blissbiz.biz', 'localhost']) {
    assert.equal(appForLocation(domain, '/', domain), 'admin')
    assert.equal(appForLocation(`admin.${domain}`, '/login', domain), 'admin')
    assert.equal(appForLocation(`platform.${domain}`, '/', domain), 'platform')
    assert.equal(appForLocation(`threads-co.${domain}`, '/p/kurta', domain), 'shop')
    assert.equal(appForLocation(`threads-co.${domain}`, '/b/owned-shop/overview', domain), 'admin')
    assert.equal(appForLocation(`not.${domain}.evil.example`, '/', domain), 'admin')
    assert.equal(appForLocation(`nested.shop.${domain}`, '/', domain), 'admin')
  }
})

test('shop links preserve HTTPS and the actual preview/development port', () => {
  assert.equal(storefrontUrl('threads-co', '', { protocol: 'https:', port: '' }, 'blissbiz.biz'), 'https://threads-co.blissbiz.biz')
  assert.equal(storefrontUrl('threads-co', '', { protocol: 'https:', port: '8443' }, 'blissbiz.biz'), 'https://threads-co.blissbiz.biz:8443')
  assert.equal(storefrontUrl('threads-co', '', { protocol: 'http:', port: '5173' }, 'localhost'), 'http://threads-co.localhost:5173')
  assert.equal(storefrontUrl('shop', 'store.example.com', { protocol: 'https:', port: '' }), 'https://store.example.com')
  assert.equal(storefrontUrl('shop', 'evil.example/path', { protocol: 'https:', port: '' }), '')
})
