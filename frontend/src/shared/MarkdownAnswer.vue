<script setup>
import { computed } from 'vue'
import InlineMarkdown from './InlineMarkdown.vue'

const props = defineProps({ text: { type: String, default: '' } })

function cells(line) {
  return line.trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim())
}

function isTableDivider(line = '') {
  return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line)
}

function startsBlock(lines, index) {
  const line = lines[index] || ''
  const next = lines[index + 1] || ''
  return !line.trim() || /^```/.test(line) || /^#{1,4}\s+/.test(line) || /^\s*([-*•]|\d+[.)])\s+/.test(line)
    || /^\s*>\s?/.test(line) || /^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)
    || (line.includes('|') && isTableDivider(next))
}

function parse(text) {
  const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n')
  const blocks = []
  let index = 0
  while (index < lines.length) {
    const line = lines[index]
    if (!line.trim()) { index += 1; continue }

    const fence = line.match(/^```([^\s]*)\s*$/)
    if (fence) {
      const body = []
      index += 1
      while (index < lines.length && !/^```/.test(lines[index])) body.push(lines[index++])
      if (index < lines.length) index += 1
      blocks.push({ type: 'code', language: fence[1], text: body.join('\n') })
      continue
    }

    const heading = line.match(/^(#{1,4})\s+(.+)$/)
    if (heading) {
      blocks.push({ type: 'heading', level: Math.min(4, heading[1].length + 1), text: heading[2] })
      index += 1
      continue
    }

    if (line.includes('|') && isTableDivider(lines[index + 1])) {
      const headers = cells(line)
      const rows = []
      index += 2
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) rows.push(cells(lines[index++]))
      blocks.push({ type: 'table', headers, rows })
      continue
    }

    const list = line.match(/^\s*([-*•]|\d+[.)])\s+(.+)$/)
    if (list) {
      const ordered = /^\d/.test(list[1])
      const items = []
      while (index < lines.length) {
        const item = lines[index].match(/^\s*([-*•]|\d+[.)])\s+(.+)$/)
        if (!item || /^\d/.test(item[1]) !== ordered) break
        items.push(item[2])
        index += 1
      }
      blocks.push({ type: ordered ? 'ol' : 'ul', items })
      continue
    }

    const quote = line.match(/^\s*>\s?(.*)$/)
    if (quote) {
      const quoted = []
      while (index < lines.length) {
        const part = lines[index].match(/^\s*>\s?(.*)$/)
        if (!part) break
        quoted.push(part[1])
        index += 1
      }
      blocks.push({ type: 'quote', text: quoted.join(' ') })
      continue
    }

    if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
      blocks.push({ type: 'rule' })
      index += 1
      continue
    }

    const paragraph = [line.trim()]
    index += 1
    while (index < lines.length && !startsBlock(lines, index)) paragraph.push(lines[index++].trim())
    blocks.push({ type: 'paragraph', text: paragraph.join(' ') })
  }
  return blocks
}

const blocks = computed(() => parse(props.text))
</script>

<template>
  <div class="markdown-answer">
    <template v-for="(block, index) in blocks" :key="index">
      <component :is="`h${block.level}`" v-if="block.type === 'heading'"><InlineMarkdown :text="block.text" /></component>
      <p v-else-if="block.type === 'paragraph'"><InlineMarkdown :text="block.text" /></p>
      <blockquote v-else-if="block.type === 'quote'"><InlineMarkdown :text="block.text" /></blockquote>
      <ul v-else-if="block.type === 'ul'">
        <li v-for="(item, itemIndex) in block.items" :key="itemIndex"><InlineMarkdown :text="item" /></li>
      </ul>
      <ol v-else-if="block.type === 'ol'">
        <li v-for="(item, itemIndex) in block.items" :key="itemIndex"><InlineMarkdown :text="item" /></li>
      </ol>
      <div v-else-if="block.type === 'table'" class="markdown-table-wrap">
        <table><thead><tr><th v-for="(cell, cellIndex) in block.headers" :key="cellIndex"><InlineMarkdown :text="cell" /></th></tr></thead>
          <tbody><tr v-for="(row, rowIndex) in block.rows" :key="rowIndex"><td v-for="(cell, cellIndex) in row" :key="cellIndex"><InlineMarkdown :text="cell" /></td></tr></tbody>
        </table>
      </div>
      <pre v-else-if="block.type === 'code'"><code>{{ block.text }}</code></pre>
      <hr v-else-if="block.type === 'rule'" />
    </template>
  </div>
</template>
