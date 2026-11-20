<script setup>
const props = defineProps({ page: { type: Number, required: true }, pageSize: { type: Number, required: true }, total: { type: Number, required: true } })
const emit = defineEmits(['go'])
const pages = () => Math.max(1, Math.ceil(props.total / props.pageSize))
</script>

<template>
  <nav v-if="pages() > 1" class="pager" aria-label="Pages">
    <button class="ghost" :disabled="page <= 1" @click="emit('go', page - 1)">‹ Previous</button>
    <span>Page {{ page }} of {{ pages() }}</span>
    <button class="ghost" :disabled="page >= pages()" @click="emit('go', page + 1)">Next ›</button>
  </nav>
</template>
