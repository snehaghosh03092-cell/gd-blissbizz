// One build serves the owner workspace, platform administration and shop subdomains.
export const baseDomain = (import.meta.env?.VITE_BASE_DOMAIN || 'localhost').toLowerCase()

export function appForLocation(hostname, pathname, domain = baseDomain) {
  const host = hostname.toLowerCase()
  if (host === `platform.${domain}`) return 'platform'
  if (pathname.startsWith('/b/')) return 'admin'
  const suffix = `.${domain}`
  const slug = host.endsWith(suffix) ? host.slice(0, -suffix.length) : ''
  return /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(slug) && slug !== 'admin' ? 'shop' : 'admin'
}

export function storefrontUrl(slug, hostname = '', location = window.location, domain = baseDomain) {
  const host = hostname || `${slug}.${domain}`
  if (!/^[a-z0-9]+(?:[.-][a-z0-9]+)*$/i.test(host)) return ''
  const protocol = location.protocol === 'https:' ? 'https:' : 'http:'
  return `${protocol}//${host}${location.port ? `:${location.port}` : ''}`
}
