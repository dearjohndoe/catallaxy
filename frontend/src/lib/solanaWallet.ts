import { create } from 'zustand'
import { getWallets } from '@wallet-standard/app'
import type { Wallet, WalletAccount } from '@wallet-standard/base'
import { SOL_CHAIN } from '../config'

/**
 * Solana wallets via Wallet Standard (Phantom, Solflare, Backpack, …): the
 * browser-side counterpart of TON Connect, no backend involved.
 */

const CONNECT = 'standard:connect'
const DISCONNECT = 'standard:disconnect'
const EVENTS = 'standard:events'
const SIGN_AND_SEND = 'solana:signAndSendTransaction'
const SIGN = 'solana:signTransaction'
const LAST_WALLET_KEY = 'ctlx-sol-wallet'

type Features = Record<string, any>

function features(w: Wallet): Features {
  return w.features as Features
}

/** Wallets that can pay on our cluster: connect + both signing features. */
export function isUsableWallet(w: Wallet): boolean {
  const f = features(w)
  return Boolean(f[CONNECT] && f[SIGN_AND_SEND] && f[SIGN])
    && w.chains.some(c => c === SOL_CHAIN || c === 'solana:mainnet' || c === 'solana:devnet')
}

interface SolanaWalletState {
  wallets: Wallet[]
  wallet: Wallet | null
  account: WalletAccount | null
  connecting: boolean
  connect: (w: Wallet) => Promise<void>
  disconnect: () => Promise<void>
}

let offChange: (() => void) | null = null

function remember(name: string | null) {
  try {
    if (name) localStorage.setItem(LAST_WALLET_KEY, name)
    else localStorage.removeItem(LAST_WALLET_KEY)
  } catch { /* storage unavailable */ }
}

export const useSolanaWallet = create<SolanaWalletState>((set, get) => ({
  wallets: [],
  wallet: null,
  account: null,
  connecting: false,

  connect: async (w, silent = false) => {
    set({ connecting: true })
    try {
      const { accounts } = await features(w)[CONNECT].connect(silent ? { silent: true } : undefined)
      const account = (accounts as WalletAccount[])[0] ?? null
      if (!account) { if (!silent) throw new Error('Wallet returned no account'); return }
      offChange?.()
      offChange = features(w)[EVENTS]?.on('change', ({ accounts: next }: { accounts?: WalletAccount[] }) => {
        if (!next) return
        set({ account: next[0] ?? null, wallet: next[0] ? w : null })
      }) ?? null
      set({ wallet: w, account })
      remember(w.name)
    } finally {
      set({ connecting: false })
    }
  },

  disconnect: async () => {
    const w = get().wallet
    offChange?.(); offChange = null
    set({ wallet: null, account: null })
    remember(null)
    try { await features(w as Wallet)?.[DISCONNECT]?.disconnect() } catch { /* best effort */ }
  },
}) as SolanaWalletState & { connect: (w: Wallet, silent?: boolean) => Promise<void> })

let started = false

/** Start listening for injected wallets; reconnects silently to the last one. */
export function initSolanaWallets(): void {
  if (started || typeof window === 'undefined') return
  started = true
  const api = getWallets()
  const refresh = () => {
    const wallets = api.get().filter(isUsableWallet)
    useSolanaWallet.setState({ wallets })
    let last: string | null = null
    try { last = localStorage.getItem(LAST_WALLET_KEY) } catch { /* ignore */ }
    const st = useSolanaWallet.getState()
    const prev = last && !st.wallet ? wallets.find(w => w.name === last) : undefined
    if (prev) {
      ;(st.connect as (w: Wallet, silent?: boolean) => Promise<void>)(prev, true).catch(() => remember(null))
    }
  }
  refresh()
  api.on('register', refresh)
  api.on('unregister', refresh)
}

function requireAccount(): { wallet: Wallet; account: WalletAccount } {
  const { wallet, account } = useSolanaWallet.getState()
  if (!wallet || !account) throw new Error('Connect a Solana wallet first')
  return { wallet, account }
}

/** Wallet signs and broadcasts; returns the raw 64-byte signature. */
export async function signAndSendTransaction(tx: Uint8Array): Promise<Uint8Array> {
  const { wallet, account } = requireAccount()
  const [out] = await features(wallet)[SIGN_AND_SEND].signAndSendTransaction({
    // No `options`: Solflare rejects keys it does not know (e.g. `commitment`).
    account, transaction: tx, chain: SOL_CHAIN,
  })
  return out.signature as Uint8Array
}

/** Wallet signs only (its own slot); used for x402 where the seller pays fees. */
export async function signTransaction(tx: Uint8Array): Promise<Uint8Array> {
  const { wallet, account } = requireAccount()
  const [out] = await features(wallet)[SIGN].signTransaction({
    account, transaction: tx, chain: SOL_CHAIN,
  })
  return out.signedTransaction as Uint8Array
}
