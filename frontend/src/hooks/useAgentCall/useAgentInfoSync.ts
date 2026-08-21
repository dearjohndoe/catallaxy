import { useEffect } from 'react'
import { fetchAgentInfo } from '../../lib/agentClient'
import { useStore } from '../../store/useStore'
import type { Agent } from '../../types'
import type { CallState } from './useCallState'

export function useAgentInfoSync(endpoint: string, sidecarId: string, expanded: boolean, s: CallState) {
  useEffect(() => {
    if (!expanded) return
    let cancelled = false
    s.setSkusLoading(true)
    fetchAgentInfo(endpoint)
      .then(info => {
        if (cancelled) return
        s.setSkus(info.skus)
        s.setPaymentRails(info.paymentRails)
        if (info.paymentRails.length > 0) {
          s.setSelectedRail(prev =>
            info.paymentRails.includes(prev) ? prev : (info.paymentRails[0] ?? 'TON')
          )
        }
        const firstAvail = info.skus.find(sk => sk.stockLeft == null || sk.stockLeft > 0)
        s.setSelectedSkuId(prev => prev || firstAvail?.id || info.skus[0]?.id || '')
        // Thin heartbeat leaves schemas/description/media off-chain; hydrate from /info.
        const patch: Partial<Agent> = {}
        if (info.name) patch.name = info.name
        if (info.description) patch.description = info.description
        if (info.capabilities?.length) patch.capabilities = info.capabilities
        if (info.argsSchema) patch.argsSchema = info.argsSchema
        if (info.resultSchema) patch.resultSchema = info.resultSchema
        if (info.hasQuote) patch.hasQuote = true
        if (info.previewUrl) patch.previewUrl = info.previewUrl
        if (info.avatarUrl) patch.avatarUrl = info.avatarUrl
        if (info.images?.length) patch.images = info.images
        if (Object.keys(patch).length) useStore.getState().patchAgent(sidecarId, patch)
      })
      .catch(() => { if (!cancelled) s.setSkus([]) })
      .finally(() => { if (!cancelled) s.setSkusLoading(false) })
    return () => { cancelled = true }
  }, [expanded, endpoint, sidecarId, s.infoRefreshNonce])

  useEffect(() => {
    if (s.status === 'done' || s.status === 'refunded') {
      s.setInfoRefreshNonce(n => n + 1)
    }
  }, [s.status])

  useEffect(() => {
    return () => {
      s.pollCancelRef.current?.()
      if (s.countdownRef.current) clearInterval(s.countdownRef.current)
    }
  }, [])
}
