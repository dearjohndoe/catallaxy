import { useState, useRef } from 'react'
import type { CallStatus } from './types'
import type { QuoteResult, PaymentOption, ConnectionMode } from '../../lib/agentClient'
import { getConnectionMode } from '../../lib/agentClient'
import type { Sku, Agent } from '../../types'
import { agentRails } from '../../lib/agents'

export function useCallState(agent: Agent) {
  const [fields, setFields] = useState<Record<string, string>>({})
  const [fileFields, setFileFields] = useState<Record<string, File>>({})
  const [status, setStatus] = useState<CallStatus>('idle')
  const [result, setResult] = useState<any>(null)
  const [errorMsg, setErrorMsg] = useState('')
  const [quote, setQuote] = useState<QuoteResult | null>(null)
  const [quoteSecondsLeft, setQuoteSecondsLeft] = useState(0)
  const [lastNonce, setLastNonce] = useState('')
  const [paymentOptions, setPaymentOptions] = useState<PaymentOption[]>([])
  const [selectedRail, setSelectedRail] = useState<string>(() => agentRails(agent)[0] ?? 'TON')
  const [paymentRails, setPaymentRails] = useState<string[]>([])
  const [connMode, setConnMode] = useState<ConnectionMode>(() => getConnectionMode(agent.endpoint))
  const [skus, setSkus] = useState<Sku[]>([])
  const [selectedSkuId, setSelectedSkuId] = useState<string>('')
  const [skusLoading, setSkusLoading] = useState(false)
  const [refundReason, setRefundReason] = useState('')
  const [refundReasonCode, setRefundReasonCode] = useState('')
  const [refundTx, setRefundTx] = useState('')
  const [infoRefreshNonce, setInfoRefreshNonce] = useState(0)
  const pollCancelRef = useRef<(() => void) | null>(null)
  const countdownRef = useRef<ReturnType<typeof setInterval> | null>(null)

  return {
    fields, setFields,
    fileFields, setFileFields,
    status, setStatus,
    result, setResult,
    errorMsg, setErrorMsg,
    quote, setQuote,
    quoteSecondsLeft, setQuoteSecondsLeft,
    lastNonce, setLastNonce,
    paymentOptions, setPaymentOptions,
    selectedRail, setSelectedRail,
    paymentRails, setPaymentRails,
    connMode, setConnMode,
    skus, setSkus,
    selectedSkuId, setSelectedSkuId,
    skusLoading, setSkusLoading,
    refundReason, setRefundReason,
    refundReasonCode, setRefundReasonCode,
    refundTx, setRefundTx,
    infoRefreshNonce, setInfoRefreshNonce,
    pollCancelRef, countdownRef,
  }
}

export type CallState = ReturnType<typeof useCallState>
