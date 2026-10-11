import { invokeAgent } from '../../../lib/agentClient'
import type { InvokeResult } from '../../../lib/agentClient'
import type { FlowResult } from '../types'

export async function runInvoke(args: {
  endpoint: string
  capability: string
  body: Record<string, string | number | boolean>
  txBoc: string
  nonce: string
  quoteId?: string
  fileFields: Record<string, File>
  rail: string
  skuId?: string
  paymentSignature?: string
}): Promise<FlowResult<InvokeResult>> {
  try {
    const res = await invokeAgent(
      args.endpoint,
      args.txBoc,
      args.nonce,
      args.capability,
      args.body,
      args.quoteId,
      args.fileFields,
      args.rail,
      args.skuId,
      args.paymentSignature,
    )
    return { kind: 'ok', value: res }
  } catch (err: any) {
    return {
      kind: 'error',
      message: err?.response?.data?.error ?? err?.message ?? 'Failed to call agent',
    }
  }
}
