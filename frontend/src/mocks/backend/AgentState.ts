/**
 * In-memory mock of the sidecar backend. Drives both the browser MSW worker
 * (clickable VITE_USE_MOCK demo) and vitest integration tests.
 *
 * State machine mirrors the real sidecar at the API contract level:
 *   - SKUs with optional finite total / sold counters
 *   - reservations keyed by quote_id or nonce, with TTL + sweep
 *   - jobs that materialise after `delayMs` based on `behavior`
 *   - atomic commit_sold / agent_out_of_stock paths
 *
 * Not modelled: on-chain payment verification, refund tx broadcast, file
 * uploads, rate limiting. Those are stubbed (txs trusted, refund tx faked).
 */

import type {
  SidecarFixture,
  SkuState,
  Reservation,
  QuoteEntry,
  JobRecord,
  PersistedAgentState,
} from './types'
import {
  PAYMENT_TIMEOUT_MS,
  QUOTE_TTL_MS,
  FAKE_REFUND_TX,
} from './constants'

export class AgentState {
  fx: SidecarFixture
  skus = new Map<string, SkuState>()
  reservations = new Map<string, Reservation>()
  quotes = new Map<string, QuoteEntry>()
  jobs = new Map<string, JobRecord>()
  private onPersist?: () => void

  constructor(fx: SidecarFixture, onPersist?: () => void) {
    this.fx = fx
    this.onPersist = onPersist
    this.reset()
  }

  reset() {
    this.skus = new Map(
      this.fx.skus.map(s => [
        s.id,
        {
          id: s.id,
          title: s.title,
          priceTon: s.priceTon,
          priceUsdt: s.priceUsdt,
          total: s.initialStock,
          sold: 0,
        },
      ])
    )
    this.reservations.clear()
    this.quotes.clear()
    this.jobs.clear()
    this.onPersist?.()
  }

  serialize(): PersistedAgentState {
    return {
      sidecarId: this.fx.sidecarId,
      skus: [...this.skus.values()].map(s => ({ id: s.id, total: s.total, sold: s.sold })),
    }
  }

  hydrate(data: PersistedAgentState) {
    for (const s of data.skus) {
      const cur = this.skus.get(s.id)
      if (cur) {
        cur.total = s.total
        cur.sold = s.sold
      }
    }
  }

  private sweep(now = Date.now()) {
    for (const [key, r] of this.reservations) {
      if (r.expiresAt <= now && !r.jobId) this.reservations.delete(key)
    }
    for (const [qid, q] of this.quotes) {
      if (q.expiresAt <= now) this.quotes.delete(qid)
    }
  }

  stockLeft(skuId: string): number | null {
    const s = this.skus.get(skuId)
    if (!s) return 0
    if (s.total == null) return null
    let reserved = 0
    for (const r of this.reservations.values()) if (r.skuId === skuId) reserved++
    return Math.max(0, s.total - s.sold - reserved)
  }

  resolveSku(skuField?: string | null): SkuState | { error: string; status: number; available?: string[] } {
    if (skuField) {
      const s = this.skus.get(skuField)
      if (!s) return { error: 'Unknown SKU', status: 400 }
      return s
    }
    if (this.skus.size === 1) return [...this.skus.values()][0]
    return {
      error: 'sku is required (multiple SKUs configured)',
      status: 400,
      available: [...this.skus.keys()],
    }
  }

  private reserve(skuId: string, key: string, ttlMs: number, now = Date.now()): boolean {
    this.sweep(now)
    const existing = this.reservations.get(key)
    if (existing) {
      if (existing.skuId !== skuId) return false
      existing.expiresAt = Math.max(existing.expiresAt, now + ttlMs)
      return true
    }
    const left = this.stockLeft(skuId)
    if (left != null && left <= 0) return false
    this.reservations.set(key, { key, skuId, expiresAt: now + ttlMs })
    return true
  }

  // ── HTTP-equivalent methods ───────────────────────────────────────

  getInfo() {
    this.sweep()
    const fx = this.fx
    const firstSku = fx.skus[0]
    const skus = [...this.skus.values()].map(s => {
      const left = this.stockLeft(s.id)
      const entry: any = { id: s.id, title: s.title }
      if (s.priceTon != null) entry.price_ton = s.priceTon
      if (s.priceUsdt != null) entry.price_usd = s.priceUsdt
      if (left != null) entry.stock_left = left
      if (s.total != null) {
        entry.total = s.total
        entry.sold = s.sold
      }
      return entry
    })
    const body: any = {
      name: fx.agent.name,
      description: fx.agent.description,
      capabilities: fx.agent.capabilities,
      price: firstSku.priceTon ?? 0,
      args_schema: fx.agent.argsSchema,
      result_schema: fx.agent.resultSchema ?? null,
      sidecar_id: fx.sidecarId,
      endpoint: fx.endpoint,
      payment_rails: fx.paymentRails,
      skus,
    }
    if (fx.agent.hasQuote) body.has_quote = true
    if (firstSku.priceUsdt != null) body.price_usdt = firstSku.priceUsdt
    if (fx.agent.previewUrl) body.preview_url = fx.agent.previewUrl
    if (fx.agent.avatarUrl) body.avatar_url = fx.agent.avatarUrl
    if (fx.agent.images) body.images = fx.agent.images
    return { status: 200, body }
  }

  postQuote(req: { capability: string; sku?: string; body: any }) {
    if (!this.fx.agent.hasQuote) return { status: 404, body: { error: 'This agent does not support quotes' } }
    if (!this.fx.agent.capabilities.includes(req.capability)) {
      return { status: 400, body: { error: 'Unsupported capability' } }
    }
    const sku = this.resolveSku(req.sku)
    if ('error' in sku) return { status: sku.status, body: { error: sku.error, available_skus: sku.available } }

    const left = this.stockLeft(sku.id)
    if (left != null && left <= 0) return { status: 409, body: { error: 'out_of_stock', sku: sku.id } }

    const quoteId = 'q_' + Math.random().toString(16).slice(2, 10)
    const ttlMs = this.fx.quotePrice?.({ skuId: sku.id, body: req.body }).ttl ?? QUOTE_TTL_MS
    const expiresAt = Date.now() + ttlMs
    const dyn = this.fx.quotePrice?.({ skuId: sku.id, body: req.body })

    const price = dyn?.price ?? sku.priceTon ?? 0
    const priceUsdt = dyn?.price_usdt ?? sku.priceUsdt

    const ok = this.reserve(sku.id, quoteId, Math.max(ttlMs, PAYMENT_TIMEOUT_MS))
    if (!ok) return { status: 409, body: { error: 'out_of_stock', sku: sku.id } }

    this.quotes.set(quoteId, {
      quoteId,
      skuId: sku.id,
      price,
      priceUsdt,
      expiresAt,
      plan: dyn?.plan,
      note: dyn?.note,
    })
    const respBody: any = {
      quote_id: quoteId,
      price,
      plan: dyn?.plan ?? '',
      sku: sku.id,
      expires_at: Math.floor(expiresAt / 1000),
    }
    if (priceUsdt) respBody.price_usdt = priceUsdt
    if (dyn?.note) respBody.note = dyn.note
    return { status: 200, body: respBody }
  }

  postInvoke(req: {
    tx?: string
    nonce: string
    capability: string
    sku?: string
    quoteId?: string
    rail?: string
    body: any
  }): { status: number; body: any } {
    if (!this.fx.agent.capabilities.includes(req.capability)) {
      return { status: 400, body: { error: 'Unsupported capability' } }
    }

    const quoteEntry = req.quoteId ? this.quotes.get(req.quoteId) : undefined
    let sku: SkuState | undefined
    if (quoteEntry) {
      sku = this.skus.get(quoteEntry.skuId)
      if (!sku) return { status: 500, body: { error: 'Quote references unknown SKU' } }
    } else {
      const r = this.resolveSku(req.sku)
      if ('error' in r) return { status: r.status, body: { error: r.error, available_skus: r.available } }
      sku = r
    }

    const rail = (req.rail ?? 'TON').toUpperCase()
    if (rail === 'TON' && sku.priceTon == null)
      return { status: 400, body: { error: 'unsupported_rail_for_sku', sku: sku.id, rail } }
    if (rail === 'USDT' && sku.priceUsdt == null)
      return { status: 400, body: { error: 'unsupported_rail_for_sku', sku: sku.id, rail } }

    // ── Preflight: no tx → 402 ───────────────────────────────────
    if (!req.tx) {
      const left = this.stockLeft(sku.id)
      if (left != null && left <= 0) return { status: 409, body: { error: 'out_of_stock', sku: sku.id } }

      const nonce = req.nonce && req.nonce.endsWith(`:${this.fx.sidecarId}`)
        ? req.nonce
        : `${Math.random().toString(16).slice(2, 18)}:${this.fx.sidecarId}`

      const wallet = this.fx.agent.wallet ?? this.fx.agent.address
      const options: any[] = []
      if (sku.priceTon != null) {
        options.push({
          rail: 'TON',
          address: wallet,
          amount: String(quoteEntry?.price ?? sku.priceTon),
          memo: nonce,
          sku: sku.id,
        })
      }
      if (sku.priceUsdt != null) {
        options.push({
          rail: 'USDT',
          address: wallet,
          amount: String(quoteEntry?.priceUsdt ?? sku.priceUsdt),
          memo: nonce,
          sku: sku.id,
          token: {
            symbol: 'USDT',
            // Real USDT master so frontend's resolveJettonWallet builds a
            // parseable cell. Mock toncenter handler still returns a fake
            // wallet address, but Address.parse() succeeds.
            master: 'EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs',
            decimals: 6,
          },
        })
      }
      const primary = options.find(o => o.rail === rail) ?? options[0]
      return {
        status: 402,
        body: {
          error: 'Payment required',
          payment_request: { address: primary.address, amount: primary.amount, memo: primary.memo, rail: primary.rail },
          payment_options: options,
        },
      }
    }

    // ── Paid invoke: needs reservation ───────────────────────────
    let reservationKey: string
    if (req.quoteId) {
      reservationKey = req.quoteId
      const r = this.reservations.get(reservationKey)
      if (!r) return { status: 400, body: { error: 'Quote not found or expired' } }
    } else {
      reservationKey = req.tx
      const ok = this.reserve(sku.id, reservationKey, PAYMENT_TIMEOUT_MS)
      if (!ok) {
        // Race-loss: would refund in real sidecar. Surface 409.
        return { status: 409, body: { error: 'out_of_stock', sku: sku.id, refund_tx: FAKE_REFUND_TX() } }
      }
    }

    const outcome = this.fx.behavior({ skuId: sku.id, body: req.body, nonce: req.nonce })
    const delayMs = outcome.delayMs ?? 0

    const jobId = 'job_' + Math.random().toString(16).slice(2, 10)
    const job: JobRecord = {
      jobId,
      reservationKey,
      finishAt: Date.now() + delayMs,
      outcome,
      status: 'pending',
    }
    this.jobs.set(jobId, job)
    const r = this.reservations.get(reservationKey)
    if (r) r.jobId = jobId

    if (delayMs <= 0) {
      this._materialise(job)
      return { status: 200, body: this._jobResponse(job) }
    }
    return { status: 200, body: { job_id: jobId, status: 'pending' } }
  }

  getResult(jobId: string) {
    const job = this.jobs.get(jobId)
    if (!job) return { status: 404, body: { error: 'Job not found' } }
    if (job.status === 'pending' && Date.now() >= job.finishAt) this._materialise(job)
    if (job.status === 'pending') return { status: 200, body: { status: 'pending' } }
    return { status: 200, body: this._jobResponse(job) }
  }

  private _materialise(job: JobRecord) {
    const o = job.outcome
    if (o.kind === 'timeout') {
      // Stays pending forever; tests can't hit this unless they advance time
      // past the timeout via the agent runner. We mark it as error after a
      // hard cap (5 * delayMs) to avoid infinite hangs in real tests.
      job.status = 'error'
      job.error = 'Agent timeout'
      this.reservations.delete(job.reservationKey)
      return
    }
    if (o.kind === 'success') {
      const r = this.reservations.get(job.reservationKey)
      if (r) {
        const s = this.skus.get(r.skuId)
        if (s) s.sold += 1
      }
      this.reservations.delete(job.reservationKey)
      job.status = 'done'
      job.result = o.result
      this.onPersist?.()
      return
    }
    if (o.kind === 'error') {
      this.reservations.delete(job.reservationKey)
      job.status = 'error'
      job.error = o.message
      return
    }
    if (o.kind === 'out_of_stock') {
      const r = this.reservations.get(job.reservationKey)
      if (r) {
        const s = this.skus.get(r.skuId)
        if (s && s.total != null) s.total = Math.max(0, s.total - 1)
      }
      this.reservations.delete(job.reservationKey)
      job.status = 'refunded'
      job.reasonCode = 'out_of_stock'
      job.reason = o.reason
      job.refundTx = FAKE_REFUND_TX()
      this.onPersist?.()
      return
    }
    if (o.kind === 'refunded') {
      this.reservations.delete(job.reservationKey)
      job.status = 'refunded'
      job.reasonCode = o.reasonCode
      job.reason = o.reason
      job.refundTx = FAKE_REFUND_TX()
      this.onPersist?.()
      return
    }
  }

  private _jobResponse(job: JobRecord) {
    if (job.status === 'done') return { job_id: job.jobId, status: 'done', result: job.result }
    if (job.status === 'refunded')
      return {
        job_id: job.jobId,
        status: 'refunded',
        reason_code: job.reasonCode,
        reason: job.reason,
        refund_tx: job.refundTx,
      }
    if (job.status === 'error') return { job_id: job.jobId, status: 'error', error: job.error }
    return { job_id: job.jobId, status: 'pending' }
  }
}
