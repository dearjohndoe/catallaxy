import { Address, toNano } from '@ton/core'
import { invokePreflight } from '../../../lib/agentClient'
import type { PaymentRequest, PaymentOption } from '../../../lib/agentClient'
import { buildPaymentPayload, buildJettonTransferPayload, bocToMsgHash, resolveJettonWallet } from '../../../lib/crypto'
import { TESTNET, TONCENTER_BASE } from '../../../config'
import type { FlowResult } from '../types'

export interface PaymentSuccess { txBoc: string; paymentRequest: PaymentRequest; rail: string; paymentOptions: PaymentOption[] }

type TonConnect = { sendTransaction: (params: any) => Promise<{ boc: string }>; account?: { address: string } | null }

async function sendUsdt(pr: PaymentRequest, options: PaymentOption[], tc: TonConnect): Promise<string> {
  const userAddr = Address.parse(tc.account?.address ?? '').toString({ bounceable: false, urlSafe: true, testOnly: TESTNET })
  const master = options.find(o => o.rail === 'USDT')?.token?.master ?? ''
  if (!master) throw new Error('USDT master address not available')
  const userJettonWallet = await resolveJettonWallet(TONCENTER_BASE, master, userAddr)
  const payload = buildJettonTransferPayload(pr.address, BigInt(pr.amount), pr.memo, userAddr)
  const res = await tc.sendTransaction({
    validUntil: Math.floor(Date.now() / 1000) + 300,
    messages: [{ address: userJettonWallet, amount: toNano('0.1').toString(), payload }],
  })
  return bocToMsgHash(res.boc)
}

async function sendTon(pr: PaymentRequest, tc: TonConnect): Promise<string> {
  const recipient = Address.parse(pr.address).toString({ bounceable: false, urlSafe: true, testOnly: TESTNET })
  const res = await tc.sendTransaction({
    validUntil: Math.floor(Date.now() / 1000) + 300,
    messages: [{ address: recipient, amount: pr.amount, payload: buildPaymentPayload(pr.memo) }],
  })
  return bocToMsgHash(res.boc)
}

export async function runPayment(args: {
  endpoint: string
  capability: string
  body: Record<string, string | number | boolean>
  quoteId?: string
  skuId?: string
  rail: string
  tonConnectUI: TonConnect
}): Promise<FlowResult<PaymentSuccess>> {
  let paymentRequest: PaymentRequest
  let rail = args.rail
  let preflightOptions: PaymentOption[] = []
  try {
    const pf = await invokePreflight(args.endpoint, args.capability, args.body, args.quoteId, args.skuId)
    paymentRequest = pf.paymentRequest
    preflightOptions = pf.paymentOptions
    const chosen = preflightOptions.find(o => o.rail === rail)
    if (!chosen) {
      const available = preflightOptions.map(o => o.rail).join(', ')
      throw new Error(`Rail "${rail}" not offered by agent for this SKU. Available: ${available || 'none'}`)
    }
    rail = chosen.rail
    paymentRequest = {
      address: chosen.address,
      amount: chosen.amount,
      nonce: chosen.nonce || chosen.memo, // claim value; fall back for old sidecars
      memo: chosen.memo,
      rail: chosen.rail,
    }
  } catch (err: any) {
    const data = err?.response?.data
    if (err?.response?.status === 409 && data?.error === 'out_of_stock') {
      return { kind: 'error', message: `Out of stock${data.sku ? ` (${data.sku})` : ''}` }
    }
    return { kind: 'error', message: err?.message ?? 'Failed to reach agent' }
  }

  let txBoc: string
  try {
    txBoc = rail === 'USDT'
      ? await sendUsdt(paymentRequest, preflightOptions, args.tonConnectUI)
      : await sendTon(paymentRequest, args.tonConnectUI)
  } catch (err: any) {
    return { kind: 'error', message: err?.message === 'Reject request' ? 'Payment cancelled' : 'Payment failed' }
  }

  return { kind: 'ok', value: { txBoc, paymentRequest, rail, paymentOptions: preflightOptions } }
}
