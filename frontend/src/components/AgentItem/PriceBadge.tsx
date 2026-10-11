import { Fragment } from 'react'
import type { Agent } from '../../types'
import { formatRailPrice } from './utils'

export function agentPrices(agent: Agent): [string, number][] {
  const out: [string, number][] = []
  if (agent.price > 0) out.push(['TON', agent.price])
  if (agent.priceUsdt) out.push(['USDT', agent.priceUsdt])
  if (agent.priceSol) out.push(['SOL', agent.priceSol])
  if (agent.priceUsdc) out.push(['USDC', agent.priceUsdc])
  return out
}

export function PriceBadge({ agent }: { agent: Agent }) {
  const prices = agentPrices(agent)
  if (!prices.length) return <span>Dynamic price</span>
  return (
    <>
      {prices.map(([rail, amount], i) => (
        <Fragment key={rail}>
          {i > 0 && <span className="price-sep"> / </span>}
          <span className={`price-${rail.toLowerCase()}`}>{formatRailPrice(rail, amount)}</span>
        </Fragment>
      ))}
    </>
  )
}
