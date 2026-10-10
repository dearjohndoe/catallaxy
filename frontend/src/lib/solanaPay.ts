import { SOL_RPC_URL } from '../config'
import { solRpc } from './solanaRpc'

/**
 * Client-side Solana payments. `@solana/web3.js` is loaded lazily so a
 * TON-only visitor does not download it.
 *
 * - SOL: SystemProgram transfer + Memo `CTLX:PAY:<pub>` (the 402 `memo`,
 *   verbatim); the wallet signs and broadcasts; proof = tx signature.
 * - USDC: x402 `exact` — TransferChecked with the seller as fee payer; the
 *   wallet only signs its slot, the sidecar co-signs and broadcasts.
 */

const MEMO_PROGRAM = 'MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr'
const TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
const ATA_PROGRAM = 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'
const USDC_DECIMALS = 6

const web3 = () => import('@solana/web3.js')

async function latestBlockhash(rpcUrl: string): Promise<string> {
  const r = await solRpc<{ value: { blockhash: string } }>(rpcUrl, 'getLatestBlockhash', [{ commitment: 'confirmed' }])
  return r.value.blockhash
}

/** Unsigned v0 tx: pay `lamports` to `to` with the 402 memo text. */
export async function buildSolPaymentTx(args: {
  payer: string
  to: string
  lamports: bigint
  memo: string
  rpcUrl?: string
}): Promise<Uint8Array> {
  const { PublicKey, SystemProgram, TransactionInstruction, TransactionMessage, VersionedTransaction } = await web3()
  const payer = new PublicKey(args.payer)
  const ixs = [
    SystemProgram.transfer({ fromPubkey: payer, toPubkey: new PublicKey(args.to), lamports: args.lamports }),
    new TransactionInstruction({
      programId: new PublicKey(MEMO_PROGRAM),
      keys: [{ pubkey: payer, isSigner: true, isWritable: false }],
      data: new TextEncoder().encode(args.memo) as any,
    }),
  ]
  const msg = new TransactionMessage({
    payerKey: payer,
    recentBlockhash: await latestBlockhash(args.rpcUrl ?? SOL_RPC_URL),
    instructions: ixs,
  }).compileToV0Message()
  return new VersionedTransaction(msg).serialize()
}

export async function associatedTokenAddress(owner: string, mint: string): Promise<string> {
  const { PublicKey } = await web3()
  const [ata] = PublicKey.findProgramAddressSync(
    [new PublicKey(owner).toBytes(), new PublicKey(TOKEN_PROGRAM).toBytes(), new PublicKey(mint).toBytes()],
    new PublicKey(ATA_PROGRAM),
  )
  return ata.toBase58()
}

/** Token balance in base units; 0 when the account does not exist. */
export async function tokenBalance(owner: string, mint: string, rpcUrl: string = SOL_RPC_URL): Promise<bigint> {
  const ata = await associatedTokenAddress(owner, mint)
  try {
    const r = await solRpc<{ value?: { amount?: string } }>(rpcUrl, 'getTokenAccountBalance', [ata, { commitment: 'confirmed' }])
    return BigInt(r?.value?.amount ?? '0')
  } catch {
    return 0n
  }
}

/** x402 `accepts[]` entry (SVM exact) as the sidecar emits it. */
export interface X402Accepts {
  scheme: string
  network: string
  amount?: string
  maxAmountRequired?: string
  asset: string
  payTo: string
  extra?: { feePayer?: string }
  [k: string]: unknown
}

export function acceptsAmount(a: X402Accepts): bigint {
  return BigInt(a.amount ?? a.maxAmountRequired ?? '0')
}

/**
 * Unsigned x402 exact tx in the sidecar's whitelisted shape:
 * SetComputeUnitLimit, SetComputeUnitPrice (spec cap: ≤ 5 lamports/CU), TransferChecked.
 * Fee payer = `extra.feePayer` (the seller); `payer` only authorizes the transfer.
 */
export async function buildX402UsdcTx(args: {
  payer: string
  accepted: X402Accepts
  rpcUrl?: string
}): Promise<Uint8Array> {
  const {
    ComputeBudgetProgram, PublicKey, TransactionInstruction, TransactionMessage, VersionedTransaction,
  } = await web3()
  const feePayer = args.accepted.extra?.feePayer
  if (!feePayer) throw new Error('x402 requirements carry no feePayer')
  const owner = new PublicKey(args.payer)
  const mint = new PublicKey(args.accepted.asset)
  const amount = acceptsAmount(args.accepted)
  const src = new PublicKey(await associatedTokenAddress(args.payer, args.accepted.asset))
  const dst = new PublicKey(await associatedTokenAddress(args.accepted.payTo, args.accepted.asset))

  const data = new Uint8Array(10)
  data[0] = 12 // TransferChecked
  new DataView(data.buffer).setBigUint64(1, amount, true)
  data[9] = USDC_DECIMALS
  const ixs = [
    ComputeBudgetProgram.setComputeUnitLimit({ units: 20_000 }), // x402 reference default; sidecar caps at 60k
    ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 1 }),
    new TransactionInstruction({
      programId: new PublicKey(TOKEN_PROGRAM),
      keys: [
        { pubkey: src, isSigner: false, isWritable: true },
        { pubkey: mint, isSigner: false, isWritable: false },
        { pubkey: dst, isSigner: false, isWritable: true },
        { pubkey: owner, isSigner: true, isWritable: false },
      ],
      data: data as any,
    }),
  ]
  const msg = new TransactionMessage({
    payerKey: new PublicKey(feePayer),
    recentBlockhash: await latestBlockhash(args.rpcUrl ?? SOL_RPC_URL),
    instructions: ixs,
  }).compileToV0Message()
  return new VersionedTransaction(msg).serialize()
}

function b64(bytes: Uint8Array): string {
  let s = ''
  for (const b of bytes) s += String.fromCharCode(b)
  return btoa(s)
}

/** `PAYMENT-SIGNATURE` header value (x402 v2) for a wallet-signed exact tx. */
export function x402PaymentHeader(signedTx: Uint8Array, accepted: X402Accepts, resourceUrl: string): string {
  const payload = {
    x402Version: 2,
    accepted,
    resource: { url: typeof accepted.resource === 'string' ? accepted.resource : resourceUrl },
    payload: { transaction: b64(signedTx) },
  }
  return b64(new TextEncoder().encode(JSON.stringify(payload)))  // UTF-8 safe, unlike btoa(str)
}
