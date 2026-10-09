// Official Catallaxy mailbox. Override with VITE_REGISTRY_ADDRESS (e2e testnet).
export const REGISTRY_ADDRESS =
  ((import.meta.env.VITE_REGISTRY_ADDRESS as string | undefined) ?? '').trim()
  || 'UQCYxSFNCJHmBxVpgfqAesgjLQDsLch3WJG3MJYyhnBDS7gg'
export const TESTNET = import.meta.env.VITE_TESTNET === 'true'
export const TONCENTER_BASE = TESTNET
  ? 'https://testnet.toncenter.com/api/v3'
  : 'https://toncenter.com/api/v3'
export const SSL_GATEWAY = (import.meta.env.VITE_SSL_GATEWAY as string) ?? ''

export const CACHE_TTL_MS = import.meta.env.DEV ? 0 : 5 * 60 * 1000
export const AGENTS_PER_PAGE = 20
export const TX_PAGE_SIZE = 100
export const PAYMENT_OPCODE = 0x50415900
export const REFUND_OPCODE = 0x52464E44
export const RATING_OPCODE = 0x52617465
export const HEARTBEAT_OPCODE = 0xAC52AB67

export const MIN_RATING_TXS = parseInt(import.meta.env.VITE_MIN_RATING_TXS || '1', 10)
