import { Address } from '@ton/core'
import { TESTNET } from '../../config'

export function nanoToTon(n: number) {
  const t = n / 1e9
  return t < 0.001 ? t.toExponential(2) : t.toFixed(3).replace(/\.?0+$/, '')
}

export function microToUsdt(n: number) {
  const t = n / 1e6
  return t < 0.01 ? t.toExponential(2) : t.toFixed(2).replace(/\.?0+$/, '')
}

export function formatAddr(raw: string): string {
  try {
    const friendly = Address.parse(raw).toString({ bounceable: false, urlSafe: true, testOnly: TESTNET })
    return `${friendly.slice(0, 7)}…${friendly.slice(-7)}`
  } catch {
    return `${raw.slice(0, 7)}…${raw.slice(-7)}`
  }
}

export function friendlyAddr(raw: string): string {
  try {
    return Address.parse(raw).toString({ bounceable: false, urlSafe: true, testOnly: TESTNET })
  } catch {
    return raw
  }
}

export function normalizeDesc(s: string): string {
  return s.replace(/\\n/g, '\n')
}

export function lamportsToSol(n: number) {
  const t = n / 1e9
  return t < 0.0001 ? t.toExponential(2) : t.toFixed(6).replace(/\.?0+$/, '')
}

/** Human price for a rail's base units ("0.01 TON", "1.5 USDC"). */
export function formatRailPrice(rail: string, amount: number): string {
  switch (rail) {
    case 'USDT': return `${microToUsdt(amount)} USDT`
    case 'USDC': return `${microToUsdt(amount)} USDC`
    case 'SOL': return `${lamportsToSol(amount)} SOL`
    default: return `${nanoToTon(amount)} TON`
  }
}
