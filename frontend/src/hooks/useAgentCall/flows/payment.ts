import { Address, toNano } from '@ton/core'
import { invokePreflight, RailsUnavailableError } from '../../../lib/agentClient'
import type { PaymentRequest, PaymentOption } from '../../../lib/agentClient'
import { buildPaymentPayload, buildJettonTransferPayload, bocToMsgHash, resolveJettonWallet } from '../../../lib/crypto'
import { TESTNET, TONCENTER_BASE } from '../../../config'
import type { FlowResult } from '../types'
import { isSolanaRail } from '../../../lib/agents'
import { base58 } from '../../../lib/solanaRpc'
import { useSolanaWallet, signAndSendTransaction, signTransaction } from '../../../lib/solanaWallet'
import {
  acceptsAmount, buildSolPaymentTx, buildX402UsdcTx, tokenBalance, x402PaymentHeader, type X402Accepts,
} from '../../../lib/solanaPay'

export interface PaymentSuccess {
  /** Proof for the claim: TON message hash, or Solana tx signature (base58). */
  txBoc: string
  paymentRequest: PaymentRequest
  rail: string
  paymentOptions: PaymentOption[]
  /** USDC/x402: `PAYMENT-SIGNATURE` value; the sidecar settles it at claim. */
  paymentSignature?: string
}

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

function solanaPayer(): string {
  const account = useSolanaWallet.getState().account
  if (!account) throw new Error('Connect a Solana wallet first')
  return account.address
}

async function sendSol(pr: PaymentRequest): Promise<string> {
  const tx = await buildSolPaymentTx({
    payer: solanaPayer(), to: pr.address, lamports: BigInt(pr.amount), memo: pr.memo,
  })
  return base58(await signAndSendTransaction(tx))
}

async function signUsdc(accepts: any[], resourceUrl: string): Promise<string> {
  const accepted = accepts.find(a => a?.scheme === 'exact' && String(a?.network ?? '').startsWith('solana')) as X402Accepts | undefined
  if (!accepted) throw new Error('Agent did not offer an x402 USDC payment')
  const payer = solanaPayer()
  const need = acceptsAmount(accepted)
  if (await tokenBalance(payer, accepted.asset) < need) throw new Error('Not enough USDC in the connected wallet')
  const signed = await signTransaction(await buildX402UsdcTx({ payer, accepted }))
  return x402PaymentHeader(signed, accepted, resourceUrl)
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
  let accepts: any[] = []
  let resourceUrl = ''
  try {
    const pf = await invokePreflight(args.endpoint, args.capability, args.body, args.quoteId, args.skuId)
    paymentRequest = pf.paymentRequest
    preflightOptions = pf.paymentOptions
    accepts = pf.accepts
    resourceUrl = pf.resourceUrl
    const chosen = preflightOptions.find(o => o.rail === rail)
    if (!chosen && pf.unavailableRails.includes(rail)) {
      throw new RailsUnavailableError([rail])
    }
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

  let txBoc = ''
  let paymentSignature: string | undefined
  try {
    if (rail === 'USDC') paymentSignature = await signUsdc(accepts, resourceUrl)
    else if (rail === 'SOL') txBoc = await sendSol(paymentRequest)
    else if (rail === 'USDT') txBoc = await sendUsdt(paymentRequest, preflightOptions, args.tonConnectUI)
    else txBoc = await sendTon(paymentRequest, args.tonConnectUI)
  } catch (err: any) {
    const msg = String(err?.message ?? '')
    if (msg === 'Reject request' || /reject|denied|cancel/i.test(msg)) return { kind: 'error', message: 'Payment cancelled' }
    // Our own preconditions (no wallet, no USDC) are worth showing as is.
    if (isSolanaRail(rail) && msg && !/^Payment/.test(msg)) return { kind: 'error', message: msg }
    return { kind: 'error', message: 'Payment failed' }
  }

  return { kind: 'ok', value: { txBoc, paymentRequest, rail, paymentOptions: preflightOptions, paymentSignature } }
}
