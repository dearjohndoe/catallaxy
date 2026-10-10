import type { Agent, Chain } from '../types'

/**
 * Merge listings by `sidecar_id` across registries (TON + Solana). The newest
 * heartbeat supplies the fields; `chains` is the union, and the TON address
 * is kept from whichever listing has one (ratings are TON transactions).
 */
export function mergeAgents(...lists: Agent[][]): Agent[] {
  const map = new Map<string, Agent>()
  for (const a of lists.flat()) {
    const cur = map.get(a.sidecarId)
    if (!cur) { map.set(a.sidecarId, a); continue }
    const [newer, older] = a.lastHeartbeat > cur.lastHeartbeat ? [a, cur] : [cur, a]
    const chains = [...new Set<Chain>([...(older.chains ?? []), ...(newer.chains ?? [])])]
    map.set(a.sidecarId, {
      ...older,
      ...newer,
      address: newer.address || older.address,
      chains,
    })
  }
  return [...map.values()].sort((a, b) => b.lastHeartbeat - a.lastHeartbeat)
}

/** Rails an agent can be paid on, from its heartbeat or (old heartbeats) its prices. */
export function agentRails(a: Agent): string[] {
  if (a.rails?.length) return a.rails
  const out: string[] = []
  if (a.price > 0) out.push('TON')
  if (a.priceUsdt) out.push('USDT')
  if (a.priceSol) out.push('SOL')
  if (a.priceUsdc) out.push('USDC')
  return out.length ? out : ['TON']
}

export const SOLANA_RAILS = new Set(['SOL', 'USDC'])

export function isSolanaRail(rail: string): boolean {
  return SOLANA_RAILS.has(rail)
}
