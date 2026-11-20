<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { shopApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import { loadCart, state } from './store.js'

const props = defineProps({ mode: { type: String, default: 'login' } })
const route = useRoute()
const router = useRouter()
const form = ref({ email: '', password: '', full_name: '', phone: '' })
const error = ref(null)
const busy = ref(false)

async function submit() {
  busy.value = true
  error.value = null
  try {
    await shopApi.refreshCsrf() // an anonymous session must exist before signing in
    const body = props.mode === 'login'
      ? { email: form.value.email, password: form.value.password }
      : { ...form.value, phone: form.value.phone || null }
    const r = await shopApi.post(props.mode === 'login' ? '/auth/login' : '/auth/register', body)
    shopApi.setCsrf(r.csrf_token) // the session was replaced: use its token
    if (r.owner && r.business) {
      // This must reload the document: /b/... boots the owner application instead of the shop.
      window.location.assign(`/b/${r.business.id}/overview`)
      return
    }
    state.customer = r.customer
    await loadCart() // the guest cart was merged into the account
    const next = String(route.query.next || '/')
    router.push(next.startsWith('/') && !next.startsWith('//') ? next : '/')
  } catch (e) {
    error.value = e.code === 'already_authenticated' ? { message: 'You are already signed in.' } : e
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="narrow">
    <h1>{{ mode === 'login' ? 'Sign in' : 'Create your account' }}</h1>
    <p class="muted">Your account is for {{ state.shop.business.name }} only.</p>
    <form class="stack" @submit.prevent="submit">
      <label v-if="mode === 'register'">Full name <input v-model="form.full_name" required maxlength="150" autocomplete="name" /></label>
      <label>Email <input v-model="form.email" type="email" required autocomplete="email" /></label>
      <label>Password <input v-model="form.password" type="password" required :minlength="mode === 'register' ? 15 : 1" maxlength="128"
                             :autocomplete="mode === 'login' ? 'current-password' : 'new-password'" />
        <small v-if="mode === 'register'" class="muted">At least 15 characters. A short sentence works well.</small></label>
      <label v-if="mode === 'register'">Phone (optional) <input v-model="form.phone" maxlength="32" autocomplete="tel" /></label>
      <ErrorBox :error="error" />
      <button :disabled="busy">{{ mode === 'login' ? 'Sign in' : 'Create account' }}</button>
    </form>
    <p v-if="mode === 'login'">New here? <RouterLink :to="{ path: '/register', query: route.query }">Create an account</RouterLink></p>
    <p v-else>Already registered? <RouterLink :to="{ path: '/login', query: route.query }">Sign in</RouterLink></p>
  </div>
</template>
