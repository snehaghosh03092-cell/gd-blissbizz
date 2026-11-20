export const storefrontTemplates = {
  fashion: { name: 'Fashion', title: 'The everyday edit', description: 'An editorial split for clothing and accessories.', colors: ['#1f2937', '#f472b6'], art: 'fashion', layout: 'editorial' },
  grocery: { name: 'Grocery', title: 'Fresh from the market', description: 'An airy shop with a compact, easy-to-scan catalog.', colors: ['#15803d', '#f59e0b'], art: 'grocery', layout: 'market' },
  food: { name: 'Bakery & food', title: 'Baked fresh, shared warm', description: 'A warm menu with a wide feature banner.', colors: ['#9a3412', '#eab308'], art: 'food', layout: 'banner' },
  beauty: { name: 'Beauty', title: 'A little everyday ritual', description: 'Centered, soft pastel styling for care collections.', colors: ['#65374e', '#eeb3c5'], art: 'beauty', layout: 'studio' },
  electronics: { name: 'Electronics', title: 'Upgrade your everyday', description: 'A clean, high-contrast layout for tech and gadgets.', colors: ['#0f172a', '#06b6d4'], art: 'electronics', layout: 'market' },
  gaming: { name: 'Gaming', title: 'Ready for the next level', description: 'A dark, vivid banner for gaming gear.', colors: ['#4c1d95', '#22d3ee'], art: 'gaming', layout: 'banner' },
  craft: { name: 'Handmade', title: 'Made slowly, made well', description: 'An editorial collection with warm tones and room for detail.', colors: ['#704736', '#e5ae85'], art: 'craft', layout: 'editorial' },
  home: { name: 'Home & living', title: 'Make yourself at home', description: 'A balanced, centered design for home collections.', colors: ['#3b5055', '#a9d9d3'], art: 'home', layout: 'studio' },
}

export function storefrontDesign(key, theme = {}) {
  const template = storefrontTemplates[key] || storefrontTemplates.fashion
  return {
    classes: [`design-${template.layout}`, `design-${template.art}`],
    colors: {
      '--shop-primary': theme.primary || template.colors[0],
      '--shop-accent': theme.accent || template.colors[1],
    },
  }
}
