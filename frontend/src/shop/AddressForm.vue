<script setup>
import { ref } from 'vue'
import { shopApi } from '../api/client.js'
import ErrorBox from '../shared/ErrorBox.vue'

const STATES = { '01': 'Jammu and Kashmir', '02': 'Himachal Pradesh', '03': 'Punjab', '04': 'Chandigarh', '05': 'Uttarakhand',
  '06': 'Haryana', '07': 'Delhi', '08': 'Rajasthan', '09': 'Uttar Pradesh', '10': 'Bihar', '11': 'Sikkim',
  '12': 'Arunachal Pradesh', '13': 'Nagaland', '14': 'Manipur', '15': 'Mizoram', '16': 'Tripura', '17': 'Meghalaya',
  '18': 'Assam', '19': 'West Bengal', '20': 'Jharkhand', '21': 'Odisha', '22': 'Chhattisgarh', '23': 'Madhya Pradesh',
  '24': 'Gujarat', '26': 'Dadra and Nagar Haveli and Daman and Diu', '27': 'Maharashtra', '29': 'Karnataka', '30': 'Goa',
  '31': 'Lakshadweep', '32': 'Kerala', '33': 'Tamil Nadu', '34': 'Puducherry', '35': 'Andaman and Nicobar Islands',
  '36': 'Telangana', '37': 'Andhra Pradesh', '38': 'Ladakh' }
const emit = defineEmits(['saved', 'cancel'])
const form = ref({ label: '', recipient: '', phone: '', line1: '', line2: '', city: '', state_code: '29', postal_code: '', is_default: false })
const error = ref(null)
const busy = ref(false)

async function save() {
  busy.value = true
  error.value = null
  try {
    const body = { ...form.value, label: form.value.label || null, line2: form.value.line2 || null }
    emit('saved', await shopApi.post('/me/addresses', body))
  } catch (e) {
    error.value = e
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <form class="stack card" @submit.prevent="save">
    <div class="grid2">
      <label>Name <input v-model="form.recipient" required maxlength="150" /></label>
      <label>Phone <input v-model="form.phone" required minlength="6" maxlength="32" /></label>
    </div>
    <label>Address line 1 <input v-model="form.line1" required maxlength="200" /></label>
    <label>Address line 2 <input v-model="form.line2" maxlength="200" /></label>
    <div class="grid3">
      <label>City <input v-model="form.city" required maxlength="80" /></label>
      <label>State <select v-model="form.state_code"><option v-for="(name, code) in STATES" :key="code" :value="code">{{ name }}</option></select></label>
      <label>PIN code <input v-model="form.postal_code" required pattern="[1-9][0-9]{5}" maxlength="6" inputmode="numeric" /></label>
    </div>
    <label class="check"><input v-model="form.is_default" type="checkbox" /> Make this my default address</label>
    <ErrorBox :error="error" :labels="{ state_code: 'State', postal_code: 'PIN code' }" />
    <div class="row"><button :disabled="busy">Save address</button><button type="button" class="ghost" @click="emit('cancel')">Cancel</button></div>
  </form>
</template>
