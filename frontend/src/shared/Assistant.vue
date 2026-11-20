<script setup>
// Chat + free browser voice input. Web Speech converts speech to text in the browser; only the
// resulting text is sent through the same grounded assistant endpoint as typed questions.
// "Speak answers" uses browser speech synthesis. Cards and links come from server tool results.
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import AppIcon from './AppIcon.vue'
import MarkdownAnswer from './MarkdownAnswer.vue'

const props = defineProps({
  api: { type: Object, required: true },
  path: { type: String, required: true }, // POST {text} here
  linkBase: { type: String, default: '' }, // owner links are relative to /b/<id>/
  examples: { type: Array, default: () => [] },
  title: { type: String, default: 'Assistant' },
  welcome: { type: Object, default: null }, // local first answer; never calls the model
  storageKey: { type: String, default: '' },
  prompt: { type: Object, default: null },
})
const emit = defineEmits(['navigate', 'prompt-consumed'])
const router = useRouter()
const turns = ref([])
const draft = ref('')
const busy = ref(false)
const recording = ref(false)
const speak = ref(false)
const log = ref(null)
const downloading = ref('')
const downloadError = ref('')
let conversation = null
let recognition = null
let disposed = false
if (props.storageKey) {
  try {
    const saved = JSON.parse(sessionStorage.getItem(props.storageKey) || '{}')
    conversation = typeof saved.conversation === 'string' ? saved.conversation : null
    turns.value = Array.isArray(saved.turns) ? saved.turns.slice(-20) : []
  } catch { /* A damaged local draft starts a fresh conversation. */ }
}
if (!turns.value.length && props.welcome?.text) {
  turns.value.push({ who: 'bot', text: props.welcome.text, links: props.welcome.links || [], local: true })
}
watch(turns, () => {
  if (props.storageKey) sessionStorage.setItem(props.storageKey, JSON.stringify({ conversation, turns: turns.value.slice(-20) }))
}, { deep: true })
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition
const voiceStatus = ref('')

onBeforeUnmount(() => {
  disposed = true
  recognition?.abort()
  if (speak.value) window.speechSynthesis?.cancel()
})

function scroll() {
  nextTick(() => log.value && (log.value.scrollTop = log.value.scrollHeight))
}

function say(text) {
  if (!speak.value || !('speechSynthesis' in window)) return
  window.speechSynthesis.cancel()
  const u = new SpeechSynthesisUtterance(text)
  u.lang = 'en-IN'
  window.speechSynthesis.speak(u)
}

async function send(body, url, shown, appendUser = true) {
  busy.value = true
  if (appendUser) turns.value.push({ who: 'you', text: shown })
  scroll()
  try {
    const r = await props.api.post(url, { ...body, ...(conversation ? { conversation_id: conversation } : {}) })
    if (disposed) return
    conversation = r.conversation_id
    if (props.storageKey) sessionStorage.setItem(props.storageKey, JSON.stringify({ conversation, turns: turns.value.slice(-20) }))
    if (shown === '🎤 …') turns.value[turns.value.length - 1].text = `🎤 ${r.transcript}`
    turns.value.push({ who: 'bot', text: r.answer, cards: r.cards, links: r.links, artifacts: r.artifacts, route: r.route })
    say(r.answer)
    if (r.ui?.navigate) {
      emit('navigate', r.ui)
      if (props.linkBase) router.push(props.linkBase + r.ui.navigate)
    }
  } catch (e) {
    turns.value.push({
      who: 'bot',
      text: e.code === 'assistant_busy'
        ? 'The guide is receiving a lot of questions. Your conversation is safe—try this question again.'
        : e.message,
      error: true,
      retry: { body, url, shown },
    })
  } finally {
    busy.value = false
    scroll()
  }
}

function retry(turn, index) {
  if (busy.value || !turn.retry) return
  turns.value.splice(index, 1)
  send(turn.retry.body, turn.retry.url, turn.retry.shown, false)
}

function ask(text) {
  const t = (text ?? draft.value).trim()
  if (!t || busy.value) return
  draft.value = ''
  send({ text: t }, props.path, t)
}

watch(() => props.prompt, (value) => {
  if (value?.text) { ask(value.text); emit('prompt-consumed') }
}, { immediate: true })

function dictate() {
  if (recognition) return recognition.stop()
  if (!SpeechRecognition) {
    voiceStatus.value = 'Voice input is unavailable in this browser. You can type your question.'
    return
  }
  voiceStatus.value = 'Allow microphone access, then wait for “Listening”.'
  window.speechSynthesis?.cancel()
  const speech = new SpeechRecognition()
  speech.lang = 'en-IN'
  speech.continuous = false
  speech.interimResults = true
  speech.maxAlternatives = 1
  speech.onstart = () => {
    recording.value = true
    voiceStatus.value = 'Listening… Speak naturally.'
  }
  speech.onresult = (event) => {
    draft.value = Array.from(event.results, (result) => result[0].transcript).join(' ').trim().slice(0, 500)
    const complete = Array.from(event.results).every((result) => result.isFinal)
    voiceStatus.value = complete ? 'Voice captured. Review it, then select Ask.' : 'Listening…'
  }
  speech.onerror = (event) => {
    if (event.error === 'network') {
      voiceStatus.value = 'The browser could not reach its speech service. Check your connection or type your question.'
      return
    }
    if (event.error === 'audio-capture') {
      voiceStatus.value = 'Chrome could not access a microphone. Check your input device and system microphone permissions.'
      return
    }
    const denied = event.error === 'not-allowed' || event.error === 'service-not-allowed'
    voiceStatus.value = denied
      ? 'Microphone access was blocked. Allow it in your browser settings or type your question.'
      : 'Could not capture speech. Please try again or type your question.'
  }
  speech.onend = () => {
    if (voiceStatus.value.startsWith('Listening')) {
      voiceStatus.value = draft.value.trim() ? 'Review your question and select Ask.' : 'No speech was detected. Tap the microphone and try again.'
    }
    recognition = null
    recording.value = false
  }
  try {
    speech.start()
    recognition = speech
  } catch {
    voiceStatus.value = 'Voice input could not start. You can type your question.'
  }
}

function linkTo(url) {
  if (!url) return ''
  return url.startsWith('/') ? url : props.linkBase + url
}

function validArtifact(file) {
  const businessId = props.path.match(/^(?:\/api\/v1)?\/admin\/businesses\/([A-Za-z0-9_-]+)\//)?.[1]
  const businessPath = businessId ? `/api/v1/admin/businesses/${businessId}` : ''
  if (!businessPath || !file?.url?.startsWith(`${businessPath}/documents/`)) return false
  try {
    const url = new URL(file.url, window.location.origin)
    return url.origin === window.location.origin && /^\/(api\/v1\/admin\/businesses\/[A-Za-z0-9_-]+)\/documents\/(invoices\.(xlsx|zip)|inventory\.xlsx|registration\.xlsx)$/.test(url.pathname)
  } catch { return false }
}

async function download(file) {
  if (!validArtifact(file) || downloading.value) return
  downloading.value = file.url
  downloadError.value = ''
  try {
    const response = await props.api.raw(file.url)
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = file.url.split('?')[0].split('/').pop()
    document.body.appendChild(link)
    link.click()
    link.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (e) {
    downloadError.value = e.message || 'The download failed. Please try again.'
  } finally {
    downloading.value = ''
  }
}

const external = (url) => /^https?:\/\//i.test(url || '')
const suggestionIcons = ['spark', 'building', 'insights', 'payments']
</script>

<template>
  <div class="assistant">
    <div class="row between"><strong v-if="title">{{ title }}</strong>
      <label class="check small"><input v-model="speak" type="checkbox" /> Speak answers</label></div>
    <div ref="log" class="chatlog" aria-live="polite">
      <div v-if="!turns.length" class="assistant-suggestions">
        <p class="assistant-suggestions-title"><AppIcon name="spark" :size="16" /> Choose a starting point</p>
        <div class="assistant-suggestion-grid">
          <button v-for="(ex, index) in examples" :key="ex" type="button" class="assistant-suggestion" @click="ask(ex)">
            <span class="assistant-suggestion-icon"><AppIcon :name="suggestionIcons[index % suggestionIcons.length]" :size="18" /></span>
            <span>{{ ex }}</span>
          </button>
        </div>
      </div>
      <div v-for="(t, n) in turns" :key="n" :class="['bubble', t.who, t.error && 'bad']">
        <MarkdownAnswer v-if="t.who === 'bot' && !t.error" :text="t.text" />
        <p v-else class="answer-text">{{ t.text }}</p>
        <button v-if="t.retry" type="button" class="assistant-retry" :disabled="busy" @click="retry(t, n)">Try again</button>
        <div v-if="t.artifacts?.some(validArtifact)" class="assistant-downloads">
          <button v-for="file in t.artifacts.filter(validArtifact)" :key="file.url" type="button" class="assistant-download" :disabled="!!downloading" @click="download(file)">
            <AppIcon name="download" :size="18" /> {{ downloading === file.url ? 'Preparing download…' : file.label }}
          </button>
          <small class="muted">Uses current shop records when downloaded.</small>
        </div>
        <div v-if="t.cards?.length" class="cards">
          <component :is="external(c.url) ? 'a' : c.url ? 'RouterLink' : 'div'" v-for="(c, i) in t.cards" :key="i"
            :to="c.url && !external(c.url) ? linkTo(c.url) : undefined" :href="external(c.url) ? c.url : undefined"
            :target="external(c.url) ? '_blank' : undefined" :rel="external(c.url) ? 'noopener' : undefined" class="minicard">
            <strong>{{ c.name || c.title || c.number || c.sku }}</strong>
            <span v-if="c.price" class="small">₹{{ Number(c.price).toLocaleString('en-IN') }} · {{ c.in_stock ? 'in stock' : 'out of stock' }}</span>
            <span v-else-if="c.available !== undefined" class="small">{{ c.sku }} · {{ c.available }} available</span>
            <span v-else-if="c.units" class="small">{{ c.units }} sold</span>
            <span v-else-if="c.action" class="small">{{ c.action }}</span>
            <span v-else-if="c.status" class="small">{{ c.status.toLowerCase() }}</span>
          </component>
        </div>
        <div v-if="t.links?.length" class="assistant-links small">
          <button v-for="l in t.links.filter((item) => item.ask)" :key="l.ask" type="button" class="followup-prompt" @click="ask(l.ask)"><AppIcon name="spark" :size="14" />{{ l.label }}</button>
          <template v-for="l in t.links.filter((item) => item.url)" :key="l.url">
            <a v-if="external(l.url)" :href="l.url" target="_blank" rel="noopener">{{ l.label }} ↗</a>
            <RouterLink v-else :to="linkTo(l.url)">{{ l.label }} ›</RouterLink>
          </template>
        </div>
      </div>
      <p v-if="busy" class="muted small">Thinking…</p>
    </div>
    <p v-if="downloadError" class="bad small" role="alert">{{ downloadError }}</p>
    <form class="row" @submit.prevent="ask()">
      <input v-model="draft" maxlength="500" placeholder="Ask a question…" aria-label="Your question" :disabled="busy" />
      <button type="button" :class="['ghost', recording && 'danger']" :disabled="busy" :aria-pressed="recording"
        :aria-label="recording ? 'Stop microphone' : 'Use microphone'" @click="dictate">{{ recording ? 'Stop' : '🎤' }}</button>
      <button :disabled="busy || recording || !draft.trim()">Ask</button>
    </form>
    <p v-if="voiceStatus" class="muted small" role="status">{{ voiceStatus }}</p>
  </div>
</template>

<style scoped>
.assistant-downloads { display: grid; gap: 8px; margin: 14px 0 4px; }
.assistant-download { display: flex; align-items: center; justify-content: flex-start; gap: 9px; text-align: left; padding: 11px 13px; border-radius: 10px; background: #f4effa; color: #554073; border: 1px solid #d8cce6; font-size: 0.9rem; font-weight: 650; }
.assistant-download:hover { background: #eae0f4; }
.assistant-downloads small { font-size: 0.75rem; }
</style>
