import { reactive } from 'vue'
import { shopApi } from '../api/client.js'

// Small shared state for the storefront (no extra library): the shop, who is signed in, the cart.
export const state = reactive({ shop: null, shopError: null, customer: null, owner: null, ownerBusiness: null, cart: null, ready: false })

export async function loadShop() {
  try {
    state.shop = await shopApi.get('')
    const theme = state.shop.theme || {}
    document.title = state.shop.business.name
    if (theme.primary) document.documentElement.style.setProperty('--brand', theme.primary)
    if (theme.accent) document.documentElement.style.setProperty('--accent', theme.accent)
  } catch (e) {
    state.shopError = e
  }
  await Promise.all([loadMe(), loadCart()])
  state.ready = true
}

export async function loadMe() {
  try {
    const me = await shopApi.get('/auth/me')
    state.customer = me.customer || null
    state.owner = me.owner || null
    state.ownerBusiness = me.business || null
  } catch {
    state.customer = null
    state.owner = null
    state.ownerBusiness = null
  }
}

export async function loadCart() {
  try {
    state.cart = await shopApi.get('/cart')
  } catch {
    state.cart = null
  }
}

export function cartCount() {
  return (state.cart?.items || []).reduce((n, i) => n + Number(i.quantity), 0)
}
