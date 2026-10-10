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

// ── Solana ─────────────────────────────────────────────────────────────
// Cluster follows VITE_TESTNET (devnet next to TON testnet) unless set.
export const SOL_CLUSTER: 'devnet' | 'mainnet' =
  ((import.meta.env.VITE_SOL_CLUSTER as string | undefined) ?? (TESTNET ? 'devnet' : 'mainnet')) === 'devnet'
    ? 'devnet' : 'mainnet'
export const SOL_RPC_URL =
  ((import.meta.env.VITE_SOL_RPC_URL as string | undefined) ?? '').trim()
  || (SOL_CLUSTER === 'devnet' ? 'https://api.devnet.solana.com' : 'https://api.mainnet-beta.solana.com')
// Mainnet registry wallet is not set up yet: no default, Solana discovery is off.
export const SOL_REGISTRY_ADDRESS =
  ((import.meta.env.VITE_SOL_REGISTRY_ADDRESS as string | undefined) ?? '').trim()
  || (SOL_CLUSTER === 'devnet' ? '8mJ49cRNj2zM1rvLXSBYAy65aSfJeS4KzeXo2jEuWYtR' : '')
// Wallet Standard chain id.
export const SOL_CHAIN = SOL_CLUSTER === 'devnet' ? 'solana:devnet' : 'solana:mainnet'
export const USDC_MINT = SOL_CLUSTER === 'devnet'
  ? '4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU'
  : 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
export const SOL_REG_MEMO_PREFIX = 'CTLX:REG:'
export const SOL_REGISTRY_LIMIT = 1000
