<script setup>
defineProps({
  title: { type: String, required: true },
  subtitle: { type: String, default: '' },
  preview: Boolean,
})
</script>

<template>
  <component :is="preview ? 'span' : 'section'" class="storefront-hero">
    <component :is="preview ? 'span' : 'div'" class="storefront-hero-copy">
      <component :is="preview ? 'strong' : 'h1'">{{ title }}</component>
      <span v-if="subtitle" class="storefront-hero-subtitle">{{ subtitle }}</span>
    </component>
    <span class="storefront-art" aria-hidden="true"><i></i><i></i><i></i></span>
  </component>
</template>
