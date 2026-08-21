import axios from 'axios'
import { Cell } from '@ton/core'
import { REGISTRY_ADDRESS, TONCENTER_BASE, HEARTBEAT_OPCODE, TX_PAGE_SIZE } from '../config'
import type { Agent } from '../types'
import { sanitizeImageUrl, sanitizeImageList } from './imageProxy'

const HEARTBEAT_OPCODE_HEX = `0x${HEARTBEAT_OPCODE.toString(16)}`

export interface Cursor {
  lt: string
}

function parseHeartbeatTx(tx: any): Agent | null {
  try {
    const msg = tx.in_msg
    if (!msg?.opcode || msg.opcode.toLowerCase() !== HEARTBEAT_OPCODE_HEX) return null

    const body = msg?.message_content?.body
    if (!body) { console.warn('[toncenter] no body', tx.hash); return null }

    const cell = Cell.fromBase64(body)
    const slice = cell.beginParse()
    slice.loadUint(32) // skip opcode — already verified above
    const payload = JSON.parse(slice.loadStringTail())
    if (!payload.endpoint || !payload.sidecar_id) return null

    const address = msg.source ?? ''
    const capabilities = Array.isArray(payload.capabilities) 
      ? payload.capabilities 
      : (payload.capability ? [payload.capability] : [])

    return {
      address,
      sidecarId: payload.sidecar_id ?? '',
      name: payload.name ?? '',
      description: payload.description ?? '',
      capabilities,
      price: Number(payload.price ?? payload.price_hint) || 0,
      priceUsdt: payload.price_usdt != null ? Number(payload.price_usdt) : undefined,
      endpoint: payload.endpoint,
      argsSchema: payload.args_schema ?? {},
      lastHeartbeat: tx.now,
      hasQuote: payload.has_quote === true,
      resultSchema: payload.result_schema ?? undefined,
      previewUrl: sanitizeImageUrl(payload.preview_url) ?? undefined,
      avatarUrl: sanitizeImageUrl(payload.avatar_url) ?? undefined,
      images: sanitizeImageList(payload.images),
    }
  } catch {
    return null
  }
}

function dedupe(agents: Agent[]): Agent[] {
  const map = new Map<string, Agent>()
  for (const a of agents) {
    const cur = map.get(a.sidecarId)
    if (!cur || a.lastHeartbeat > cur.lastHeartbeat) map.set(a.sidecarId, a)
  }
  return [...map.values()].sort((a, b) => b.lastHeartbeat - a.lastHeartbeat)
}

export async function fetchAgentPage(cursor?: Cursor): Promise<{
  agents: Agent[]
  hasMore: boolean
  nextCursor: Cursor | null
}> {
  const sevenDaysAgo = Math.floor(Date.now() / 1000) - 7 * 24 * 60 * 60

  const params: Record<string, string | number | boolean> = {
    account: REGISTRY_ADDRESS,
    limit: TX_PAGE_SIZE,
    sort: 'desc',
    archival: true,
  }
  if (cursor) {
    params.end_lt = cursor.lt
  }

  const { data } = await axios.get(`${TONCENTER_BASE}/transactions`, { params })
  const txs: any[] = data.transactions ?? []

  const fresh = txs.filter(tx => tx.now >= sevenDaysAgo)
  const reachedOld = fresh.length < txs.length

  const agents = dedupe(fresh.map(parseHeartbeatTx).filter((a): a is Agent => a !== null))

  const last = txs[txs.length - 1]
  const nextCursor: Cursor | null =
    !reachedOld && txs.length === TX_PAGE_SIZE && last
      ? { lt: last.lt }
      : null

  return { agents, hasMore: nextCursor !== null, nextCursor }
}
