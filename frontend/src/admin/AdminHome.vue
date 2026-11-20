<script setup>
import { useRouter } from 'vue-router'
import StatusBadge from '../shared/StatusBadge.vue'
import AppIcon from '../shared/AppIcon.vue'
import { session } from './store.js'

const router = useRouter()
session.current = null
if (session.businesses.length === 1) router.replace(`/b/${session.businesses[0].id}`)
</script>

<template>
  <main class="page starter-home">
    <template v-if="!session.businesses.length">
      <header class="launch-page">
        <h1>What would you like to do?</h1>
        <p>Start with a conversation, or create your website when you are ready.</p>
      </header>
      <div class="launch-options">
        <RouterLink class="launch-choice" to="/guide">
          <span class="section-icon"><AppIcon name="assistant" /></span>
          <h2>Talk to your agent</h2>
          <p>Explore business ideas, paperwork, startup costs and pricing. You can type or use your microphone.</p>
        </RouterLink>
        <RouterLink class="launch-choice" to="/create">
          <span class="section-icon"><AppIcon name="shop" /></span>
          <h2>Create a website</h2>
          <p>Choose a template, name your shop and make it yours.</p>
        </RouterLink>
      </div>
    </template>
    <template v-else>
    <h1>Your businesses</h1>
    <div class="grid">
      <RouterLink v-for="b in session.businesses" :key="b.id" :to="`/b/${b.id}`" class="card">
        <h3>{{ b.name }}</h3><p class="muted">{{ b.slug }} · {{ b.role.toLowerCase() }}</p><StatusBadge :status="b.status" />
      </RouterLink>
    </div>
    </template>
  </main>
</template>
