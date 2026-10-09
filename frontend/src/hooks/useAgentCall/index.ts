import type { FormEvent } from 'react'
import type { Agent, Sku } from '../../types'
import { useCallState } from './useCallState'
import { useGatewayMode } from './useGatewayMode'
import { useAgentInfoSync } from './useAgentInfoSync'
import { useQuoteCountdown } from './useQuoteCountdown'
import { buildBody } from './flows/buildBody'
import { runQuote } from './flows/quote'
import { runPayment } from './flows/payment'
import { runInvoke } from './flows/invoke'
import { runFreeInvoke } from './flows/freeInvoke'
import { startPolling } from './flows/pollResult'

export type { CallStatus } from './types'

export function useAgentCall(
  agent: Agent,
  expanded: boolean,
  tonConnectUI: { sendTransaction: (params: any) => Promise<{ boc: string }>; account?: { address: string } | null },
) {
  const s = useCallState(agent)
  useGatewayMode(agent.endpoint, expanded, s.setConnMode)
  useAgentInfoSync(agent.endpoint, agent.sidecarId, expanded, s)
  useQuoteCountdown(s.status, s.quote, s.setQuoteSecondsLeft, s.countdownRef)

  const selectedSku: Sku | null = s.skus.find(sk => sk.id === s.selectedSkuId) ?? null
  const capability = agent.capabilities[0] ?? ''

  async function handleGetQuote(e: FormEvent) {
    e.preventDefault()
    s.setStatus('quoting'); s.setErrorMsg(''); s.setQuote(null)
    const r = await runQuote({
      endpoint: agent.endpoint, capability,
      body: buildBody(s.fields, agent.argsSchema),
      skuId: s.selectedSkuId || undefined,
    })
    if (r.kind === 'error') { s.setStatus('error'); s.setErrorMsg(r.message); return }
    s.setQuote(r.value)
    if (r.value.plan && typeof r.value.plan === 'object' && 'quote_id' in r.value.plan) {
      const planQuoteId = (r.value.plan as { quote_id: string }).quote_id
      s.setFields(f => ({ ...f, quote_id: planQuoteId }))
    }
    s.setStatus('quoted')
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    s.setErrorMsg(''); s.setResult(null)

    const body = buildBody(s.fields, agent.argsSchema)

    // Free SKU: no payment, no rail selection — call the agent directly.
    if (selectedSku?.free) {
      s.setStatus('invoking')
      const fr = await runFreeInvoke({
        endpoint: agent.endpoint, capability, body,
        skuId: selectedSku.id, fileFields: s.fileFields,
      })
      if (fr.kind === 'error') { s.setStatus('error'); s.setErrorMsg(fr.message); return }
      applyInvokeResult(fr.value)
      return
    }

    s.setStatus('paying')
    const pay = await runPayment({
      endpoint: agent.endpoint, capability, body,
      quoteId: s.quote?.quoteId, skuId: s.selectedSkuId || undefined,
      rail: s.selectedRail, tonConnectUI,
    })
    if (pay.kind === 'error') { s.setStatus('error'); s.setErrorMsg(pay.message); return }
    s.setPaymentOptions(pay.value.paymentOptions)
    // Rating payload is on-chain — only the public memo, never the claim secret.
    s.setLastNonce(pay.value.paymentRequest.memo)

    s.setStatus('invoking')
    const inv = await runInvoke({
      endpoint: agent.endpoint, capability, body,
      txBoc: pay.value.txBoc, nonce: pay.value.paymentRequest.nonce,
      quoteId: s.quote?.quoteId, fileFields: s.fileFields,
      rail: pay.value.rail, skuId: s.selectedSkuId || undefined,
    })
    if (inv.kind === 'error') { s.setStatus('error'); s.setErrorMsg(inv.message); return }

    applyInvokeResult(inv.value)
  }

  function applyInvokeResult(res: import('../../lib/agentClient').InvokeResult) {
    if (res.status === 'done') { s.setResult(res.result); s.setStatus('done') }
    else if (res.status === 'refunded') {
      s.setRefundReason(res.reason ?? '')
      s.setRefundReasonCode(res.reasonCode ?? '')
      s.setRefundTx(res.refundTx ?? '')
      s.setStatus('refunded')
    } else if (res.status === 'error') {
      s.setStatus('error'); s.setErrorMsg(res.error ?? 'Agent returned an error')
    } else {
      s.setStatus('polling')
      s.pollCancelRef.current = startPolling(agent.endpoint, res.jobId, {
        onDone: (r) => { s.setResult(r); s.setStatus('done') },
        onRefund: (reason, reasonCode, tx) => {
          s.setRefundReason(reason)
          s.setRefundReasonCode(reasonCode)
          s.setRefundTx(tx)
          s.setStatus('refunded')
        },
        onError: (msg) => { s.setStatus('error'); s.setErrorMsg(msg) },
      })
    }
  }

  function reset() {
    s.pollCancelRef.current?.(); s.pollCancelRef.current = null
    s.setStatus('idle'); s.setResult(null); s.setQuote(null)
    s.setLastNonce(''); s.setPaymentOptions([])
    s.setRefundReason(''); s.setRefundReasonCode(''); s.setRefundTx('')
  }

  function resetQuote() { s.setStatus('idle'); s.setQuote(null) }

  const busy = s.status === 'quoting' || s.status === 'paying' || s.status === 'invoking' || s.status === 'polling'
  const hasSchema = Object.keys(agent.argsSchema).length > 0

  return {
    fields: s.fields, setFields: s.setFields,
    fileFields: s.fileFields, setFileFields: s.setFileFields,
    status: s.status, result: s.result, errorMsg: s.errorMsg,
    quote: s.quote, quoteSecondsLeft: s.quoteSecondsLeft,
    lastNonce: s.lastNonce, connMode: s.connMode,
    paymentOptions: s.paymentOptions,
    selectedRail: s.selectedRail, setSelectedRail: s.setSelectedRail,
    paymentRails: s.paymentRails,
    skus: s.skus, skusLoading: s.skusLoading,
    selectedSkuId: s.selectedSkuId, setSelectedSkuId: s.setSelectedSkuId,
    selectedSku,
    refundReason: s.refundReason, refundReasonCode: s.refundReasonCode, refundTx: s.refundTx,
    busy, hasSchema,
    handleGetQuote, handleSubmit, reset, resetQuote,
  }
}
