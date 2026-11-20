// A recommendation link selects the exact size/colour it discounted, including out-of-stock options.
export function initialVariant(variants, requestedId) {
  return variants.find((variant) => variant.id === requestedId)
    || variants.find((variant) => variant.in_stock)
    || variants[0]
    || null
}
