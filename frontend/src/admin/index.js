import App from './AdminApp.vue'
import { loadSession, session } from './store.js'

export { App }
export const routes = [
  { path: '/login', component: () => import('./AdminLogin.vue'), meta: { public: true } },
  { path: '/signup', component: () => import('./AdminSignup.vue'), meta: { public: true } },
  { path: '/guide', component: () => import('./AdminAccountGuide.vue') },
  { path: '/create', component: () => import('./AdminCreateWebsite.vue') },
  { path: '/', component: () => import('./AdminHome.vue') },
  {
    path: '/b/:bid',
    component: () => import('./AdminBusiness.vue'),
    children: [
      { path: '', component: () => import('./AdminLaunch.vue') },
      { path: 'overview', component: () => import('./AdminDashboard.vue') },
      { path: 'setup', component: () => import('./AdminSetup.vue') },
      { path: 'assistant', component: () => import('./AdminAssistant.vue') },
      { path: 'shop-assistant', redirect: (to) => `/b/${to.params.bid}/assistant` },
      { path: 'insights', redirect: (to) => `/b/${to.params.bid}/overview` },
      { path: 'orders', component: () => import('./AdminOrders.vue') },
      { path: 'orders/:oid', component: () => import('./AdminOrder.vue') },
      { path: 'returns', component: () => import('./AdminReturns.vue') },
      { path: 'products', component: () => import('./AdminProducts.vue') },
      { path: 'products/:pid', component: () => import('./AdminProduct.vue') },
      { path: 'inventory', component: () => import('./AdminInventory.vue') },
      { path: 'suppliers', component: () => import('./AdminSuppliers.vue') },
      { path: 'purchase-orders', component: () => import('./AdminPurchaseOrders.vue') },
      { path: 'purchase-orders/:poid', component: () => import('./AdminPurchaseOrder.vue') },
      { path: 'supplier-invoices', component: () => import('./AdminSupplierInvoices.vue') },
      { path: 'payments', component: () => import('./AdminPayments.vue') },
      { path: 'invoices', component: () => import('./AdminInvoices.vue') },
      { path: 'invoices/:iid', component: () => import('./AdminInvoice.vue') },
    ],
  },
  { path: '/:rest(.*)*', redirect: '/' },
]

export function setupRouter(router) {
  router.beforeEach(guard)
}

async function guard(to) {
  if (!session.checked) await loadSession()
  if (!to.meta.public && !session.owner) return { path: '/login', query: { next: to.fullPath } }
  return true
}
