<script setup>
// Shows a server error with its per-field details (for example "Only 2 left").
defineProps({ error: { type: Object, default: null }, labels: { type: Object, default: () => ({}) } })
</script>

<template>
  <div v-if="error" class="error" role="alert">
    <strong>{{ error.message }}</strong>
    <ul v-if="error.fields && Object.keys(error.fields).length">
      <li v-for="(msg, key) in error.fields" :key="key"><span v-if="labels[key] || key.length < 30">{{ labels[key] || key }}: </span>{{ msg }}</li>
    </ul>
  </div>
</template>
