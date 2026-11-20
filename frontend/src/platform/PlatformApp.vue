<script setup>
import { useRouter } from 'vue-router'
import { adminApi } from '../api/client.js'
import { me } from './index.js'

const router = useRouter()
async function logout() {
  await adminApi.post('/auth/logout')
  adminApi.setCsrf(null)
  me.owner = null
  router.push('/login')
}
</script>

<template>
  <div class="admin">
    <header class="topbar dark">
      <RouterLink to="/" class="brand">BlissBiz platform</RouterLink>
      <nav v-if="me.owner" class="row"><RouterLink to="/">Businesses</RouterLink><RouterLink to="/audit">Audit log</RouterLink>
        <span class="muted">{{ me.owner.full_name }}</span><a href="#" @click.prevent="logout">Sign out</a></nav>
    </header>
    <main class="page"><RouterView /></main>
  </div>
</template>
