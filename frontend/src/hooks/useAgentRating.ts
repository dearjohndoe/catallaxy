import { useState, useEffect, useCallback } from 'react'
import { fetchOnChainRating, type OnChainRating } from '../lib/rating'

const USE_MOCK = import.meta.env.VITE_USE_MOCK === 'true'
const RATING_CACHE_TTL = 2 * 60 * 60 * 1000 // 2 hours

const MOCK_RATINGS: Record<string, OnChainRating> = {
  'mock-1': { score: 8.4, totalTxs: 47, reviews: 3, status: 'ready' },
  'mock-2': { score: 5.1, totalTxs: 12, reviews: 1, status: 'ready' },
  'mock-3': { score: 2.3, totalTxs: 8, reviews: 2, status: 'ready' },
  'mock-4': { score: null, totalTxs: 0, reviews: 0, status: 'empty' },
  'mock-5': { score: null, totalTxs: 2, reviews: 0, status: 'new' },
  'mock-6': { score: 7.1, totalTxs: 19, reviews: 2, status: 'ready' },
  'mock-7': { score: null, totalTxs: 1, reviews: 0, status: 'new' },
}

interface CachedRating {
  data: OnChainRating
  ts: number
}

function getCached(sidecarId: string): CachedRating | null {
  try {
    const raw = localStorage.getItem(`rating:${sidecarId}`)
    if (!raw) return null
    const cached: CachedRating = JSON.parse(raw)
    if (Date.now() - cached.ts > RATING_CACHE_TTL) return null
    return cached
  } catch {
    return null
  }
}

function setCache(sidecarId: string, data: OnChainRating) {
  localStorage.setItem(`rating:${sidecarId}`, JSON.stringify({ data, ts: Date.now() }))
}

export function useAgentRating(agentAddress: string, sidecarId: string, enabled: boolean) {
  const [rating, setRating] = useState<OnChainRating | null>(() => getCached(sidecarId)?.data ?? null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(false)

  const load = useCallback(async (force = false) => {
    // Solana-only agent: ratings are TON transactions, there is nothing to read.
    if (!agentAddress && !USE_MOCK) return
    if (!force) {
      const cached = getCached(sidecarId)
      if (cached) {
        setRating(cached.data)
        return
      }
    }

    setLoading(true)
    setError(false)
    try {
      if (USE_MOCK) {
        await new Promise(r => setTimeout(r, 600))
        const data = MOCK_RATINGS[sidecarId] ?? { score: null, totalTxs: 0, reviews: 0, status: 'empty' as const }
        setRating(data)
        setCache(sidecarId, data)
      } else {
        const data = await fetchOnChainRating(agentAddress, sidecarId)
        setRating(data)
        setCache(sidecarId, data)
      }
    } catch {
      setError(true)
    } finally {
      setLoading(false)
    }
  }, [agentAddress, sidecarId])

  useEffect(() => {
    if (enabled) load()
  }, [enabled, load])

  const refresh = useCallback(() => load(true), [load])

  return { rating, loading, error, refresh }
}
