<script setup>
import { STATUS_TONE, words } from './format.js'
defineProps({ status: { type: String, default: '' } })
</script>

<template>
  <span class="badge" :class="STATUS_TONE[status] || 'muted'">{{ words(status) }}</span>
</template>
