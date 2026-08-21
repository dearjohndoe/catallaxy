import { http, HttpResponse } from 'msw'
import { MockSidecarBackend, type AgentState } from './backend'
import { FIXTURES } from './fixtures'
import { beginCell, Address } from '@ton/core'

export const backend = new MockSidecarBackend(FIXTURES)

/**
 * Resolve target sidecar from either:
 *  - direct request to its endpoint (request.url origin matches fixture endpoint)
 *  - request via SSL-gateway proxy (X-Agent-Endpoint header)
 *  - request via SSL-gateway proxy (?endpoint=... query param, used by /download)
 */
function resolveAgent(request: Request): AgentState | null {
  const headerEndpoint = request.headers.get('X-Agent-Endpoint')
  if (headerEndpoint) {
    const a = backend.resolveByEndpoint(headerEndpoint)
    if (a) return a
  }
  const url = new URL(request.url)
  const queryEndpoint = url.searchParams.get('endpoint')
  if (queryEndpoint) {
    const a = backend.resolveByEndpoint(queryEndpoint)
    if (a) return a
  }
  return backend.resolve(request.url)
}

async function readMultipart(request: Request) {
  const ct = request.headers.get('Content-Type') ?? ''
  if (ct.includes('multipart/form-data')) {
    const form = await request.formData()
    const out: Record<string, any> = {}
    let bodyJson: any = {}
    form.forEach((value, key) => {
      if (key === 'body_json' && typeof value === 'string') {
        try { bodyJson = JSON.parse(value) } catch { bodyJson = {} }
      } else if (typeof value === 'string') {
        out[key] = value
      }
    })
    return { ...out, body: bodyJson } as Record<string, any>
  }
  // JSON fallback (some clients post JSON to /quote)
  try {
    const data = await request.json() as any
    return { ...data, body: data?.body ?? {} }
  } catch {
    return { body: {} }
  }
}

export const handlers = [
  // ── /info ─────────────────────────────────────────────────────
  http.get(/\/info$/, ({ request }) => {
    const agent = resolveAgent(request)
    if (!agent) return new HttpResponse(null, { status: 404 })
    const r = agent.getInfo()
    return HttpResponse.json(r.body, { status: r.status })
  }),

  // ── /quote ────────────────────────────────────────────────────
  http.post(/\/quote$/, async ({ request }) => {
    const agent = resolveAgent(request)
    if (!agent) return new HttpResponse(null, { status: 404 })
    const parsed = await readMultipart(request)
    const r = agent.postQuote({
      capability: String(parsed.capability ?? ''),
      sku: parsed.sku ? String(parsed.sku) : undefined,
      body: parsed.body ?? {},
    })
    return HttpResponse.json(r.body, { status: r.status })
  }),

  // ── /invoke (preflight + paid) ────────────────────────────────
  http.post(/\/invoke$/, async ({ request }) => {
    const agent = resolveAgent(request)
    if (!agent) return new HttpResponse(null, { status: 404 })
    const parsed = await readMultipart(request)
    const r = agent.postInvoke({
      tx: String(parsed.proof || parsed.tx || '') || undefined,
      nonce: String(parsed.nonce ?? ''),
      capability: String(parsed.capability ?? ''),
      sku: parsed.sku ? String(parsed.sku) : undefined,
      quoteId: parsed.quote_id ? String(parsed.quote_id) : undefined,
      rail: parsed.rail ? String(parsed.rail) : undefined,
      body: parsed.body ?? {},
    })
    return HttpResponse.json(r.body, { status: r.status })
  }),

  // ── /result/:jobId ────────────────────────────────────────────
  http.get(/\/result\/[^/]+$/, ({ request }) => {
    const agent = resolveAgent(request)
    if (!agent) return new HttpResponse(null, { status: 404 })
    const url = new URL(request.url)
    const jobId = url.pathname.split('/').pop() ?? ''
    const r = agent.getResult(jobId)
    return HttpResponse.json(r.body, { status: r.status })
  }),

  // ── ssl-gateway /health ───────────────────────────────────────
  http.get(/\/health$/, () => HttpResponse.json({ ok: true })),

  // ── toncenter v3 runGetMethod (jetton wallet resolve) ─────────
  // Returns a deterministic fake jetton wallet — we don't try to derive it
  // from inputs since the mock doesn't model real jetton math.
  http.post(/toncenter\.com\/api\/v3\/runGetMethod$/, async () => {
    const fakeJettonWallet = new Address(0, Buffer.alloc(32, 0xcd))
    const cell = beginCell().storeAddress(fakeJettonWallet).endCell()
    const value = cell.toBoc().toString('base64')
    return HttpResponse.json({ stack: [{ type: 'cell', value }] })
  }),

  // ── toncenter transactions list (rating fetch, agent paging) ──
  // Cheap stub: return empty list so rating / paging gracefully no-op.
  http.get(/toncenter\.com\/api\/v3\/transactions/, () =>
    HttpResponse.json({ transactions: [] })
  ),
]
