import { useEffect, useRef, useState } from 'react'
import { useSolanaWallet } from '../lib/solanaWallet'

function short(addr: string) {
  return `${addr.slice(0, 4)}…${addr.slice(-4)}`
}

/** Header button for Solana wallets (Wallet Standard), next to TON Connect. */
export function SolanaConnectButton() {
  const { wallets, account, wallet, connecting, connect, disconnect } = useSolanaWallet()
  const [open, setOpen] = useState(false)
  const [error, setError] = useState('')
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  async function pick(i: number) {
    setError('')
    try {
      await connect(wallets[i])
      setOpen(false)
    } catch (e: any) {
      setError(e?.message ?? 'Connection failed')
    }
  }

  return (
    <div className="sol-connect" ref={ref}>
      <button type="button" className="btn-agent sol-connect-btn" onClick={() => setOpen(o => !o)} disabled={connecting}>
        {account ? (
          <>
            {wallet?.icon && <img src={wallet.icon} alt="" className="sol-wallet-icon" />}
            {short(account.address)}
          </>
        ) : connecting ? 'Connecting…' : 'Solana'}
      </button>
      {open && (
        <div className="sol-connect-menu">
          {account ? (
            <button type="button" className="sol-connect-item" onClick={() => { disconnect(); setOpen(false) }}>
              Disconnect
            </button>
          ) : wallets.length === 0 ? (
            <div className="sol-connect-empty">
              No Solana wallet found. Install <a className="link" href="https://phantom.com" target="_blank" rel="noopener noreferrer">Phantom</a>,{' '}
              <a className="link" href="https://solflare.com" target="_blank" rel="noopener noreferrer">Solflare</a> or{' '}
              <a className="link" href="https://backpack.app" target="_blank" rel="noopener noreferrer">Backpack</a>.
            </div>
          ) : (
            wallets.map((w, i) => (
              <button key={w.name} type="button" className="sol-connect-item" onClick={() => pick(i)}>
                <img src={w.icon} alt="" className="sol-wallet-icon" /> {w.name}
              </button>
            ))
          )}
          {error && <div className="sol-connect-error">{error}</div>}
        </div>
      )}
    </div>
  )
}
