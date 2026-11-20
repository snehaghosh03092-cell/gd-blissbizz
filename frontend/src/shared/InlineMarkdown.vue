<script setup>
import { computed } from 'vue'

const props = defineProps({ text: { type: String, default: '' } })

const tokens = computed(() => {
  const source = props.text || ''
  const pattern = /(\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|\*([^*]+)\*)/g
  const output = []
  let cursor = 0
  for (const match of source.matchAll(pattern)) {
    if (match.index > cursor) output.push({ type: 'text', value: source.slice(cursor, match.index) })
    if (match[2]) output.push({ type: 'strong', value: match[2] })
    else if (match[3]) output.push({ type: 'code', value: match[3] })
    else if (match[4]) output.push({ type: 'link', value: match[4], url: match[5] })
    else if (match[6]) output.push({ type: 'em', value: match[6] })
    cursor = match.index + match[0].length
  }
  if (cursor < source.length) output.push({ type: 'text', value: source.slice(cursor) })
  return output
})
</script>

<template>
  <template v-for="(token, index) in tokens" :key="index">
    <strong v-if="token.type === 'strong'">{{ token.value }}</strong>
    <code v-else-if="token.type === 'code'">{{ token.value }}</code>
    <em v-else-if="token.type === 'em'">{{ token.value }}</em>
    <a v-else-if="token.type === 'link'" :href="token.url" target="_blank" rel="noopener">{{ token.value }}</a>
    <template v-else>{{ token.value }}</template>
  </template>
</template>
