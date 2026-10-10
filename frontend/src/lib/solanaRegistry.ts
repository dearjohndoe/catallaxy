import { SOL_REGISTRY_ADDRESS, SOL_REGISTRY_LIMIT, SOL_REG_MEMO_PREFIX, SOL_RPC_URL } from '../config'
import type { Agent } from '../types'
import { sanitizeImageUrl } from './imageProxy'
import { solRpc } from './solanaRpc'

const WEEK_SECONDS = 7 * 24 * 60 * 60

/**
 * Memos from `getSignaturesForAddress`'s `memo` field: `[len] text` joined by
 * `"; "`. `len` is in UTF-8 bytes, so JSON containing `"; "` still splits right.
 * Mirror of sidecar `chains/solana/registry.py::split_rendered_memos`.
 */
export function splitRenderedMemos(field: unknown): string[] {
  if (typeof field !== 'string' || !field) return []
  const raw = new TextEncoder().encode(field)
  const dec = new TextDecoder()
  const out: string[] = []
  let pos = 0
  while (pos < raw.length) {
    if (raw[pos] !== 0x5b /* [ */) return [...out, dec.decode(raw.subarray(pos))]
    let close = pos + 1
    while (close < raw.length && raw[close] >= 0x30 && raw[close] <= 0x39) close++
    if (close === pos + 1 || raw[close] !== 0x5d /* ] */ || raw[close + 1] !== 0x20) {
      return [...out, dec.decode(raw.subarray(pos))]
    }
    const n = parseInt(dec.decode(raw.subarray(pos + 1, close)), 10)
    const start = close + 2
    out.push(dec.decode(raw.subarray(start, start + n)))
    pos = start + n
    if (raw[pos] === 0x3b && raw[pos + 1] === 0x20) pos += 2
  }
  return out
}

export function parseRegMemo(field: unknown): Record<string, any> | null {
  for (const memo of splitRenderedMemos(field)) {
    if (!memo.startsWith(SOL_REG_MEMO_PREFIX)) continue
    try {
      const payload = JSON.parse(memo.slice(SOL_REG_MEMO_PREFIX.length))
      if (payload && typeof payload === 'object' && !Array.isArray(payload)) return payload
    } catch { /* not ours */ }
  }
  return null
}

function num(v: unknown): number | undefined {
  return typeof v === 'number' && Number.isFinite(v) ? v : undefined
}

export function heartbeatToAgent(payload: Record<string, any>, blockTime: number): Agent | null {
  if (typeof payload.sidecar_id !== 'string' || typeof payload.endpoint !== 'string') return null
  const capabilities = Array.isArray(payload.capabilities) ? payload.capabilities.map(String) : []
  return {
    address: '',
    sidecarId: payload.sidecar_id,
    name: typeof payload.name === 'string' ? payload.name : '',
    description: '',
    capabilities,
    price: Number(payload.price ?? payload.price_hint) || 0,
    priceUsdt: num(payload.price_usdt),
    priceSol: num(payload.price_sol),
    priceUsdc: num(payload.price_usdc),
    rails: Array.isArray(payload.rails) ? payload.rails.map((r: unknown) => String(r).toUpperCase()) : undefined,
    endpoint: payload.endpoint,
    argsSchema: {},
    lastHeartbeat: blockTime,
    hasQuote: payload.has_quote === true,
    previewUrl: sanitizeImageUrl(payload.preview_url) ?? undefined,
    avatarUrl: sanitizeImageUrl(payload.avatar_url) ?? undefined,
    chains: ['solana'],
  }
}

/**
 * Agents that heartbeated into the Solana registry in the last 7 days. One
 * `getSignaturesForAddress` call: the memo is read straight from the listing.
 * Empty when no registry is configured for this cluster.
 */
export async function fetchSolanaAgents(
  registry: string = SOL_REGISTRY_ADDRESS,
  rpcUrl: string = SOL_RPC_URL,
): Promise<Agent[]> {
  if (!registry) return []
  const rows = await solRpc<any[]>(rpcUrl, 'getSignaturesForAddress', [
    registry, { limit: SOL_REGISTRY_LIMIT, commitment: 'confirmed' },
  ])
  const cutoff = Math.floor(Date.now() / 1000) - WEEK_SECONDS
  const bySidecar = new Map<string, Agent>()
  for (const row of [...(rows ?? [])].reverse()) { // oldest first: newer heartbeat wins
    if (!row || row.err != null) continue
    const blockTime = typeof row.blockTime === 'number' ? row.blockTime : 0
    if (blockTime < cutoff) continue
    const payload = parseRegMemo(row.memo)
    const agent = payload ? heartbeatToAgent(payload, blockTime) : null
    if (agent) bySidecar.set(agent.sidecarId, agent)
  }
  return [...bySidecar.values()]
}
