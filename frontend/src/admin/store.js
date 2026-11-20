import { reactive } from 'vue'
import { adminApi } from '../api/client.js'

export const session = reactive({
  owner: null,
  businesses: [],
  checked: false,
  current: null,
  guideOpen: false,
  guidePrompt: null,
  navCollapsed: localStorage.getItem('blissbiz:nav-collapsed:v2') !== '0',
})

export function toggleWorkspaceNav() {
  session.navCollapsed = !session.navCollapsed
  localStorage.setItem('blissbiz:nav-collapsed:v2', session.navCollapsed ? '1' : '0')
}

export async function loadSession() {
  try {
    const me = await adminApi.get('/auth/me')
    session.owner = me.owner
    session.businesses = me.businesses
  } catch {
    session.owner = null
    session.businesses = []
  }
  session.checked = true
}

// The business in the URL, with its role permissions (the server re-checks every call).
export async function enterBusiness(bid, force = false) {
  if (!force && session.current?.id === bid) return session.current
  session.current = await adminApi.get(`/admin/businesses/${bid}`)
  return session.current
}

export const can = (permission) => !!session.current?.permissions?.includes(permission)
export const base = (bid) => `/admin/businesses/${bid}`

