<script setup>
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { adminApi } from '../api/client.js'
import AppIcon from '../shared/AppIcon.vue'
import Assistant from '../shared/Assistant.vue'
import { base, session } from './store.js'

const route = useRoute()
const router = useRouter()
onMounted(() => {
  if (session.current?.status !== 'ONBOARDING') {
    session.guideOpen = true
    router.replace(`/b/${route.params.bid}/overview`)
  }
})
const businessGuide = computed(() => !route.path.endsWith('/shop-assistant'))
const examples = ['what is low on stock?', 'sales this week', 'best sellers this month', 'orders to ship',
  'what should I reorder?', 'what is not selling?', 'GST collected this month', 'how do I increase sales?']
</script>

<template>
  <section v-if="businessGuide" class="guide-welcome">
    <div class="guide-welcome-copy">
      <h1>Make a plan for your business</h1>
      <p>Explore ideas that fit your budget, find out what paperwork applies, and work out how to price your first products.</p>
      <button type="button" class="guide-primary" @click="session.guideOpen = true"><AppIcon name="assistant" /> Open Business Guide</button>
    </div>
    <div class="idea-orb" aria-hidden="true"><span></span><i></i><b></b></div>
  </section>
  <header v-else class="assistant-page-head">
    <span class="assistant-page-icon"><AppIcon name="assistant" :size="28" /></span>
    <div><p class="eyebrow">YOUR SHOP, IN PLAIN WORDS</p><h1>Shop assistant</h1>
      <p>Ask about your live shop data. The assistant can look things up, but never changes anything.</p></div>
  </header>
  <Assistant v-if="!businessGuide" class="assistant-page-panel" :api="adminApi" :path="`${base(route.params.bid)}/agent`"
    :link-base="`/b/${route.params.bid}/`" :examples="examples" title="Ask your shop assistant" />
</template>
