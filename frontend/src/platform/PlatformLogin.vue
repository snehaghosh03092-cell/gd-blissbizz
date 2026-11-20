<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { adminApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import { me } from './index.js'

const router = useRouter()
const email = ref('')
const password = ref('')
const error = ref(null)

async function submit() {
  error.value = null
  try {
    await adminApi.refreshCsrf()
    const r = await adminApi.post('/auth/login', { email: email.value, password: password.value })
    adminApi.setCsrf(r.csrf_token)
    if (r.owner.platform_role !== 'admin') {
      await adminApi.post('/auth/logout')
      adminApi.setCsrf(null)
      throw new Error('This account is not a platform administrator.')
    }
    Object.assign(me, { owner: r.owner, checked: true })
    router.push('/')
  } catch (e) {
    error.value = e
  }
}
</script>

<template>
  <div class="narrow">
    <h1>Platform administration</h1>
    <form class="stack" @submit.prevent="submit">
      <label>Email <input v-model="email" type="email" required autocomplete="email" /></label>
      <label>Password <input v-model="password" type="password" required autocomplete="current-password" /></label>
      <ErrorBox :error="error" />
      <button>Sign in</button>
    </form>
    <p class="muted small">Demo: platform@blissbiz.example · REDACTED-EXAMPLE-CREDENTIAL</p>
  </div>
</template>
