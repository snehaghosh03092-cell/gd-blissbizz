<script setup>
const props = defineProps({
  name: { type: String, required: true },
  size: { type: Number, default: 20 },
})

const paths = {
  home: 'M3 10.8 12 3l9 7.8V21a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1V10.8Z',
  setup: 'M12 3a9 9 0 1 0 9 9M12 7v5l3 2M17 3v4h4',
  assistant: 'M12 3a8 8 0 0 0-8 8v1a4 4 0 0 0 4 4h1v-6H6a6 6 0 0 1 12 0h-3v6h3a4 4 0 0 1-4 4h-2',
  insights: 'M4 20V10m6 10V4m6 16v-7m5 7H3',
  orders: 'M6 3h12l2 5-2 13H6L4 8l2-5Zm-2 5h16M9 12h6',
  returns: 'M9 7 4 12l5 5M4 12h10a6 6 0 0 1 6 6',
  download: 'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5',
  invoice: 'M6 3h9l3 3v15H6V3Zm9 0v4h4M9 11h6M9 15h6',
  products: 'm12 3 8 4.5v9L12 21l-8-4.5v-9L12 3Zm-8 4.5 8 4.5 8-4.5M12 12v9',
  inventory: 'M4 7h16v13H4V7Zm2-4h12v4M8 11h8',
  suppliers: 'M3 20h18M5 20v-9h6v9m2 0V4h6v16M7 14h2M15 8h2m-2 4h2m-2 4h2',
  payments: 'M3 7h18v12H3V7Zm0 4h18M7 16h4',
  shop: 'M4 10v10h16V10M3 10l2-6h14l2 6M8 20v-6h8v6M3 10c0 2 4 2 4 0 0 2 4 2 4 0 0 2 4 2 4 0 0 2 4 2 4 0',
  check: 'm5 12 4 4L19 6',
  building: 'M4 21h16M6 21V5h12v16M9 9h2m2 0h2m-6 4h2m2 0h2m-6 4h2m2 0h2',
  spark: 'm12 3 1.2 4.8L18 9l-4.8 1.2L12 15l-1.2-4.8L6 9l4.8-1.2L12 3Zm6 11 .7 2.3L21 17l-2.3.7L18 20l-.7-2.3L15 17l2.3-.7L18 14Z',
  external: 'M14 4h6v6m0-6-9 9M19 13v7H4V5h7',
  logout: 'M10 5H5v14h5m4-3 4-4-4-4m4 4H9',
  collapse: 'm15 18-6-6 6-6',
  chevronDown: 'm7 9 5 5 5-5',
  menu: 'M4 7h16M4 12h16M4 17h16',
  close: 'm6 6 12 12M18 6 6 18',
}
</script>

<template>
  <svg
    class="app-icon"
    :width="props.size"
    :height="props.size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.8"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path :d="paths[props.name] || paths.spark" />
  </svg>
</template>
