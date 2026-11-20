<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { adminApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import { session } from './store.js'

const route = useRoute()
const router = useRouter()
const identifier = ref('')
const password = ref('')
const error = ref(null)
const busy = ref(false)

async function submit() {
  const value = identifier.value.trim()
  const valid = value.includes('@')
    ? /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)
    : /^[a-z0-9][a-z0-9_]{2,31}$/i.test(value)
  if (!valid) {
    error.value = { message: 'Enter a valid email address or username.' }
    return
  }
  busy.value = true
  error.value = null
  try {
    await adminApi.refreshCsrf()
    const r = await adminApi.post('/auth/login', { email: value, password: password.value })
    adminApi.setCsrf(r.csrf_token)
    Object.assign(session, { owner: r.owner, businesses: r.businesses, checked: true })
    const next = String(route.query.next || '/')
    router.push(next.startsWith('/') && !next.startsWith('//') ? next : '/')
  } catch (e) {
    error.value = e.code === 'already_authenticated' ? { message: 'Already signed in.' } : e
    if (e.code === 'already_authenticated') router.push('/')
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="page narrow auth-card">
    <h1>Welcome back</h1>
    <p class="muted">Sign in to your BlissBiz workspace.</p>
    <form class="stack" @submit.prevent="submit">
      <label>Email or username <input v-model="identifier" required maxlength="254" autocomplete="username" /></label>
      <label>Password <input v-model="password" type="password" required maxlength="128" autocomplete="current-password" /></label>
      <ErrorBox :error="error" />
      <button :disabled="busy">Sign in</button>
    </form>
    <p>New here? <RouterLink to="/signup">Start your online shop</RouterLink></p>
  </main>
</template>
