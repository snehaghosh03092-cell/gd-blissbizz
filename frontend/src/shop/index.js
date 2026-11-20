import App from './ShopApp.vue'

export { App }
export const routes = [
  { path: '/', component: () => import('./ShopHome.vue') },
  { path: '/p/:slug', component: () => import('./ShopProduct.vue') },
  { path: '/cart', component: () => import('./ShopCart.vue') },
  { path: '/login', component: () => import('./ShopAuth.vue'), props: { mode: 'login' } },
  { path: '/register', component: () => import('./ShopAuth.vue'), props: { mode: 'register' } },
  { path: '/checkout', component: () => import('./ShopCheckout.vue') },
  { path: '/orders', component: () => import('./ShopOrders.vue') },
  { path: '/orders/:id', component: () => import('./ShopOrder.vue') },
  { path: '/orders/:id/invoice', component: () => import('./ShopInvoice.vue') },
  { path: '/policies', component: () => import('./ShopPolicies.vue') },
  { path: '/:rest(.*)*', component: () => import('./ShopHome.vue') },
]
