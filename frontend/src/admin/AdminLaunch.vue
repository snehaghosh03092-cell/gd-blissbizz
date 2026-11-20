<script setup>
import { useRoute } from 'vue-router'
import AppIcon from '../shared/AppIcon.vue'
import { can, session } from './store.js'

const route = useRoute()
</script>

<template>
  <main class="launch-page">
    <header>
      <h1>Start building your business</h1>
      <p>Explore your idea with Business Guide or choose a design for your website.</p>
    </header>
    <div class="launch-options">
      <RouterLink v-if="can('agent.use')" class="launch-choice" :to="`/b/${route.params.bid}/assistant`" @click="session.guideOpen = true">
        <span class="section-icon"><AppIcon name="assistant" /></span>
        <h2>Talk to your agent</h2>
        <p>Work through business ideas, paperwork, startup costs and pricing. Type or use your microphone.</p>
      </RouterLink>
      <RouterLink v-if="can('business.read')" class="launch-choice" :to="`/b/${route.params.bid}/setup`">
        <span class="section-icon"><AppIcon name="shop" /></span>
        <h2>Create a website</h2>
        <p>Browse storefront templates, personalize your design, and publish when you’re ready.</p>
      </RouterLink>
    </div>
  </main>
</template>
