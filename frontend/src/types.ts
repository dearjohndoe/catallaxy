export interface ArgSchema {
  type: 'string' | 'number' | 'boolean' | 'file'
  description: string
  required: boolean
}

export type ResultType = 'string' | 'int' | 'float' | 'file' | 'url' | 'bagid' | 'json'

export interface ResultSchema {
  type: ResultType
  mime_type?: string
  encoding?: string
}

export interface TypedResult {
  type: ResultType
  data: any
  url?: string
  mime_type?: string
  file_name?: string
  expires_in?: number
}

export interface Sku {
  id: string
  title?: string
  free?: boolean         // free product: no on-chain payment
  priceTon?: number      // nanotons
  priceUsdt?: number     // micro-USDT (6 decimals)
  priceSol?: number      // lamports
  priceUsdc?: number     // micro-USDC (6 decimals)
  stockLeft?: number     // undefined = untracked / infinite
  total?: number
  sold?: number
}

export type Chain = 'ton' | 'solana'

export interface Agent {
  address: string       // heartbeat sender: TON raw address, or Solana pubkey for Solana-only agents
  sidecarId: string     // unique per sidecar instance, used as dedup key
  name: string
  description: string
  capabilities: string[]
  price: number         // nanotons
  priceUsdt?: number    // micro-USDT (6 decimals)
  endpoint: string
  argsSchema: Record<string, ArgSchema>
  lastHeartbeat: number // unix timestamp
  hasQuote?: boolean
  resultSchema?: ResultSchema
  previewUrl?: string
  avatarUrl?: string
  images?: string[]
  chains?: Chain[]      // registries this agent was found in
  rails?: string[]      // payment rails from the heartbeat (TON, USDT, SOL, USDC)
  priceSol?: number     // lamports
  priceUsdc?: number    // micro-USDC (6 decimals)
}

