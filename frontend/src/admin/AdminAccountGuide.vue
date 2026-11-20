<script setup>
import Assistant from '../shared/Assistant.vue'
import AppIcon from '../shared/AppIcon.vue'
import { adminApi } from '../api/client.js'
import { session } from './store.js'

const examples = [
  'I have ₹1 lakh but no business idea. What can I start?',
  'What papers and registrations do I need?',
  'How can I test demand before spending money?',
  'How should I price products for profit?',
]
</script>

<template>
  <main class="starter-guide">
    <header class="assistant-page-head">
      <span class="assistant-page-icon"><AppIcon name="assistant" :size="28" /></span>
      <div><p class="eyebrow">BUSINESS GUIDE</p><h1>Plan before you build</h1>
        <p>Ask about business ideas, registrations, GST, pricing and starting small.</p></div>
    </header>
    <Assistant class="assistant-page-panel starter-chat" :api="adminApi" path="/business-guide" :examples="examples"
      title="What are you thinking of building?" :storage-key="`blissbiz-guide:${session.owner.id}`" />
    <p class="guide-note"><AppIcon name="check" :size="16" /> General guidance only. Confirm legal and tax steps on the linked official portals.</p>
  </main>
</template>
