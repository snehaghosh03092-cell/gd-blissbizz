<script setup>
import { baseDomain } from '../shared/hosts.js'
import { ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { adminApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'
import { session } from './store.js'

const router = useRouter()
const form = ref({ name: '', slug: '' })
const slugTouched = ref(false)
const availability = ref(null)
const error = ref(null)
const busy = ref(false)
let timer = null
const slugify = (text) => text.toLowerCase().normalize('NFKD').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 63)
watch(() => form.value.name, (name) => { if (!slugTouched.value) form.value.slug = slugify(name) })
watch(() => form.value.slug, (slug) => {
  clearTimeout(timer)
  availability.value = null
  if (slug.length < 3) return
  timer = setTimeout(async () => {
    const result = await adminApi.get('/auth/slug', { value: slug }).catch(() => null)
    if (form.value.slug === slug) availability.value = result
  }, 300)
})
async function submit() {
  busy.value = true; error.value = null
  try {
    const business = await adminApi.post('/admin/businesses', form.value)
    session.businesses = [...session.businesses, business]
    router.push(`/b/${business.id}/setup`)
  } catch (e) { error.value = e } finally { busy.value = false }
}
</script>

<template>
  <main class="page narrow auth-card starter-create">
    <h1>Create your website</h1>
    <p class="muted">Give your shop a name and choose its local web address. You will choose a template next.</p>
    <form class="stack" @submit.prevent="submit">
      <label>Business name <input v-model="form.name" required maxlength="150" autocomplete="organization" /></label>
      <label>Shop web address
        <span class="row"><input v-model="form.slug" required minlength="3" maxlength="63" pattern="[a-z0-9][a-z0-9\-]*[a-z0-9]" @input="slugTouched = true" /><span class="muted">.{{ baseDomain }}</span></span>
        <small v-if="availability" :class="availability.available ? 'ok' : 'bad'">{{ availability.available ? `${availability.slug} is free` : availability.reason === 'taken' ? 'Already taken' : 'Not allowed; try another' }}</small>
      </label>
      <ErrorBox :error="error" :labels="{ 'business.slug': 'Web address' }" />
      <button :disabled="busy || (availability && !availability.available)">Choose a template</button>
    </form>
  </main>
</template>
