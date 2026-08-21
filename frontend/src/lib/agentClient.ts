import axios from 'axios'
import { SSL_GATEWAY } from '../config'
import type { ArgSchema, ResultSchema, Sku, TypedResult } from '../types'
import { sanitizeImageList, sanitizeImageUrl } from './imageProxy'

/**
 * Decides whether to call the agent directly or via ssl-gateway.
 * Direct when:
 *  - frontend itself is on HTTP (local dev)
 *  - agent endpoint is already HTTPS
 * Via gateway when:
 *  - frontend is on HTTPS (TMA / GitHub Pages) AND agent is on HTTP AND gateway is reachable
 */
export type ConnectionMode = 'direct' | 'proxy' | 'insecure'

// Gateway availability flag — updated by checkGatewayHealth()
let _gatewayAvailable = false
let _healthPromise: Promise<void> | null = null

export async function checkGatewayHealth(): Promise<void> {
  if (!SSL_GATEWAY) return
  if (_healthPromise) return _healthPromise
  _healthPromise = (async () => {
    try {
      await axios.get(`${SSL_GATEWAY}/health`, { timeout: 5000 })
      _gatewayAvailable = true
    } catch {
      _gatewayAvailable = false
    } finally {
      _healthPromise = null
    }
  })()
  return _healthPromise
}

export function getConnectionMode(endpoint: string): ConnectionMode {
  const frontendIsHttps = window.location.protocol === 'https:'
  const agentIsHttps = endpoint.startsWith('https://')

  if (agentIsHttps) return 'direct'
  if (frontendIsHttps && SSL_GATEWAY && _gatewayAvailable) return 'proxy'
  return 'insecure'
}

function resolveUrl(endpoint: string, path: string): { url: string; headers?: Record<string, string> } {
  const mode = getConnectionMode(endpoint)

  if (mode !== 'proxy') {
    return { url: `${endpoint}${path}` }
  }

  return {
    url: `${SSL_GATEWAY}${path}`,
    headers: { 'X-Agent-Endpoint': endpoint },
  }
}

export interface InvokeResult {
  jobId: string
  status: 'done' | 'pending' | 'error' | 'refunded'
  result?: TypedResult
  error?: string
  reason?: string
  reasonCode?: string
  refundTx?: string
}

export interface PaymentRequest {
  address: string
  amount: string
  /** Full claim nonce (`pub+sec:sidecar_id`). Never put this on-chain. */
  nonce: string
  /** Public-only half (`pub:sidecar_id`). Goes in the payment cell / rating. */
  memo: string
  rail?: string
}

export interface PaymentOption {
  rail: string
  address: string
  amount: string
  memo: string
  /** Full claim value. Distinct from `memo`. */
  nonce?: string
  token?: { symbol: string; master: string; decimals: number }
}

function buildMultipart(
  fields: Record<string, string>,
  body: Record<string, string | number | boolean>,
  fileFields?: Record<string, File>,
): FormData {
  const form = new FormData()
  for (const [k, v] of Object.entries(fields)) {
    if (v !== undefined && v !== null && v !== '') form.append(k, v)
  }
  form.append('body_json', JSON.stringify(body))
  if (fileFields) {
    for (const [name, file] of Object.entries(fileFields)) {
      form.append(`file:${name}`, file, file.name)
    }
  }
  return form
}

export interface PreflightResult {
  paymentRequest: PaymentRequest
  paymentOptions: PaymentOption[]
}

export async function invokePreflight(
  endpoint: string,
  capability: string,
  body: Record<string, string | number | boolean>,
  quoteId?: string,
  sku?: string,
): Promise<PreflightResult> {
  const form = buildMultipart({
    capability,
    ...(quoteId ? { quote_id: quoteId } : {}),
    ...(sku ? { sku } : {}),
  }, body)
  const { url, headers } = resolveUrl(endpoint, '/invoke')
  try {
    await axios.post(url, form, { timeout: 90000, headers })
    throw new Error('Expected 402 Payment Required, but got success')
  } catch (err: any) {
    if (err.response?.status === 402 && err.response.data?.payment_request) {
      const pr = err.response.data.payment_request
      const options: PaymentOption[] = err.response.data.payment_options ?? [{
        rail: pr.rail ?? 'TON',
        address: pr.address,
        amount: pr.amount,
        memo: pr.memo,
        nonce: pr.nonce,
      }]
      const primary = options[0] ?? pr
      const memo = String(primary.memo ?? pr.memo ?? '')
      const claimNonce = String(primary.nonce ?? pr.nonce ?? memo)
      return {
        paymentRequest: {
          address: pr.address,
          amount: pr.amount,
          nonce: claimNonce,
          memo,
          rail: pr.rail,
        },
        paymentOptions: options,
      }
    }
    throw err?.response?.data?.error ? new Error(err.response.data.error) : err
  }
}

export async function invokeAgent(
  endpoint: string,
  tx: string,
  nonce: string,
  capability: string,
  body: Record<string, string | number | boolean>,
  quoteId?: string,
  fileFields?: Record<string, File>,
  rail?: string,
  sku?: string,
): Promise<InvokeResult> {
  const form = buildMultipart(
    {
      // `proof` is v2; `tx` is the CTLX/1 alias so mixed-version sidecars work.
      proof: tx, tx, nonce, capability,
      ...(quoteId ? { quote_id: quoteId } : {}),
      ...(rail ? { rail } : {}),
      ...(sku ? { sku } : {}),
    },
    body,
    fileFields,
  )
  const { url, headers } = resolveUrl(endpoint, '/invoke')
  const { data } = await axios.post(url, form, { timeout: 90000, headers })
  return {
    jobId: data.job_id, status: data.status,
    result: data.result, error: data.error,
    reason: data.reason, reasonCode: data.reason_code, refundTx: data.refund_tx,
  }
}

/**
 * Invoke a FREE SKU: no on-chain payment, no preflight. The body and files are
 * sent in one shot; the sidecar gates abuse per client IP server-side, so no
 * extra identity is sent from here.
 */
export async function invokeFree(
  endpoint: string,
  capability: string,
  body: Record<string, string | number | boolean>,
  sku: string,
  fileFields?: Record<string, File>,
): Promise<InvokeResult> {
  const form = buildMultipart({ capability, sku }, body, fileFields)
  const { url, headers } = resolveUrl(endpoint, '/invoke')
  const { data } = await axios.post(url, form, { timeout: 90000, headers })
  return {
    jobId: data.job_id, status: data.status,
    result: data.result, error: data.error,
    reason: data.reason, reasonCode: data.reason_code, refundTx: data.refund_tx,
  }
}

export async function pollResult(endpoint: string, jobId: string): Promise<InvokeResult> {
  const { url, headers } = resolveUrl(endpoint, `/result/${jobId}`)
  const { data } = await axios.get(url, { timeout: 10000, headers })
  return {
    jobId, status: data.status,
    result: data.result, error: data.error,
    reason: data.reason, reasonCode: data.reason_code, refundTx: data.refund_tx,
  }
}

export interface QuotePlanStep {
  step: number
  agent: string
  capability: string
  price_ton: string
}

export interface QuotePlan {
  quote_id: string
  steps: QuotePlanStep[]
  orchestrator_fee_ton: string
  network_fees_ton: string
  total_price_ton: string
}

export interface QuoteResult {
  quoteId: string
  price: number
  priceUsdt: number | null
  plan: QuotePlan | string | null
  expiresAt: number
  note: string | null
}

export async function fetchQuote(
  endpoint: string,
  capability: string,
  body: Record<string, string | number | boolean>,
  sku?: string,
): Promise<QuoteResult> {
  const form = buildMultipart({ capability, ...(sku ? { sku } : {}) }, body)
  const { url, headers } = resolveUrl(endpoint, '/quote')
  const { data } = await axios.post(url, form, { timeout: 60000, headers })
  return {
    quoteId: data.quote_id,
    price: data.price,
    priceUsdt: typeof data.price_usdt === 'number' ? data.price_usdt : null,
    plan: data.plan,
    expiresAt: data.expires_at,
    note: data.note ?? null,
  }
}

export interface AgentInfo {
  skus: Sku[]
  paymentRails: string[]
  name?: string
  description?: string
  capabilities?: string[]
  argsSchema?: Record<string, ArgSchema>
  resultSchema?: ResultSchema
  hasQuote?: boolean
  previewUrl?: string
  avatarUrl?: string
  images?: string[]
}

export async function fetchAgentInfo(endpoint: string): Promise<AgentInfo> {
  await checkGatewayHealth()
  const { url, headers } = resolveUrl(endpoint, '/info')
  const { data } = await axios.get(url, { timeout: 8000, headers })
  const skus: Sku[] = Array.isArray(data?.skus)
    ? data.skus.map((s: any) => ({
        id: String(s.id),
        title: s.title ? String(s.title) : undefined,
        free: s.free === true ? true : undefined,
        priceTon: typeof s.price_ton === 'number' ? s.price_ton : undefined,
        priceUsdt: typeof s.price_usd === 'number' ? s.price_usd : undefined,
        stockLeft: typeof s.stock_left === 'number' ? s.stock_left : undefined,
        total: typeof s.total === 'number' ? s.total : undefined,
        sold: typeof s.sold === 'number' ? s.sold : undefined,
      }))
    : []
  const paymentRails: string[] = Array.isArray(data?.payment_rails) ? data.payment_rails : []
  return {
    skus,
    paymentRails,
    name: typeof data?.name === 'string' ? data.name : undefined,
    description: typeof data?.description === 'string' ? data.description : undefined,
    capabilities: Array.isArray(data?.capabilities) ? data.capabilities.map(String) : undefined,
    argsSchema: data?.args_schema && typeof data.args_schema === 'object' ? data.args_schema : undefined,
    resultSchema: data?.result_schema && typeof data.result_schema === 'object' ? data.result_schema : undefined,
    hasQuote: data?.has_quote === true ? true : undefined,
    previewUrl: sanitizeImageUrl(data?.preview_url) ?? undefined,
    avatarUrl: sanitizeImageUrl(data?.avatar_url) ?? undefined,
    images: sanitizeImageList(data?.images),
  }
}

export async function pingAgent(endpoint: string): Promise<boolean> {
  try {
    await checkGatewayHealth()
    const { url, headers } = resolveUrl(endpoint, '/info')
    await axios.get(url, { timeout: 5000, headers })
    return true
  } catch {
    return false
  }
}

export function resolveDownloadUrl(endpoint: string, path: string): string {
  if (path.startsWith('http://') || path.startsWith('https://')) {
    // Absolute URL from a sub-agent — derive endpoint and path, then apply
    // the same proxy logic as for relative paths.
    const parsed = new URL(path)
    const absEndpoint = parsed.origin
    const absPath = parsed.pathname + parsed.search
    const mode = getConnectionMode(absEndpoint)
    if (mode !== 'proxy') {
      return path
    }
    return `${SSL_GATEWAY}${absPath}?endpoint=${encodeURIComponent(absEndpoint)}`
  }
  const mode = getConnectionMode(endpoint)
  if (mode !== 'proxy') {
    return `${endpoint}${path}`
  }
  return `${SSL_GATEWAY}${path}?endpoint=${encodeURIComponent(endpoint)}`
}
