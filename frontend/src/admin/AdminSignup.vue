<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { adminApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import { session } from './store.js'

const router = useRouter()
const form = ref({ full_name: '', username: '', email: '', password: '', phone: '' })
const error = ref(null)
const busy = ref(false)

async function submit() {
  busy.value = true
  error.value = null
  try {
    await adminApi.refreshCsrf()
    const r = await adminApi.post('/auth/register', { ...form.value, phone: form.value.phone || null })
    adminApi.setCsrf(r.csrf_token)
    Object.assign(session, { owner: r.owner, businesses: r.businesses, checked: true, current: null })
    router.push('/')
  } catch (e) {
    error.value = e
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="page narrow auth-card">
    <h1>Create your account</h1>
    <p class="muted">Set up your workspace to explore ideas and build your website.</p>
    <form class="stack" @submit.prevent="submit">
      <label>Your name <input v-model="form.full_name" required maxlength="150" autocomplete="name" /></label>
      <label>Username <input v-model.trim="form.username" required minlength="3" maxlength="32" pattern="[A-Za-z0-9][A-Za-z0-9_]{2,31}" autocomplete="username" />
        <small class="muted">Use 3–32 letters, digits or underscores. Start with a letter or digit.</small></label>
      <label>Email <input v-model="form.email" type="email" required autocomplete="email" /></label>
      <label>Password <input v-model="form.password" type="password" required minlength="15" maxlength="128" autocomplete="new-password" />
        <small class="muted">At least 15 characters. A short sentence works well.</small></label>
      <label>Phone (optional) <input v-model="form.phone" maxlength="32" autocomplete="tel" /></label>
      <ErrorBox :error="error" :labels="{ 'owner.email': 'Email', 'owner.password': 'Password' }" />
      <button :disabled="busy">Create account</button>
    </form>
    <p>Already have an account? <RouterLink to="/login">Sign in</RouterLink></p>
  </main>
</template>
