import { useWalletUI, useWalletAddress } from '../../lib/wallet'
import { resolveDownloadUrl } from '../../lib/agentClient'
import { ResultRenderer } from '../ResultRenderer'
import type { Agent } from '../../types'
import { TESTNET } from '../../config'
import { useAgentRating } from '../../hooks/useAgentRating'
import { useAgentCall } from '../../hooks/useAgentCall'
import { useAgentReview } from '../../hooks/useAgentReview'
import { useAgentOnline } from '../../hooks/useAgentOnline'
import { RatingBlock } from '../RatingBlock'

import { PriceBadge } from './PriceBadge'
import { ConnectionBadge } from './ConnectionBadge'
import { AgentThumb } from './AgentThumb'
import { AgentGallery } from './AgentGallery'
import { AgentDesc } from './AgentDesc'
import { ShareButton } from './ShareButton'
import { CopyButton } from './CopyButton'
import { InputFields } from './InputFields'
import { SkuSelector } from './SkuSelector'
import { RailSelector } from './RailSelector'
import { StockBadge } from './StockBadge'
import { RefundedBlock } from './RefundedBlock'
import { friendlyAddr, formatAddr, nanoToTon, microToUsdt, formatRailPrice } from './utils'
import { isSolanaRail } from '../../lib/agents'
import { useSolanaWallet } from '../../lib/solanaWallet'

interface Props {
  agent: Agent
  expanded: boolean
  onToggle: () => void
  locked?: boolean
}

export function AgentItem({ agent, expanded, onToggle, locked }: Props) {
  const tonConnectUI = useWalletUI()
  const tonAddress = useWalletAddress()
  const solAccount = useSolanaWallet(st => st.account)

  const call = useAgentCall(agent, expanded, tonConnectUI)
  const { rating: onChainRating, loading: ratingLoading, error: ratingError, refresh: ratingRefresh } = useAgentRating(agent.address, agent.sidecarId, expanded)
  const review = useAgentReview(agent, call.lastNonce, tonConnectUI, ratingRefresh)
  const { online, pinging, recheck } = useAgentOnline(agent.endpoint, expanded)

  function handleReset() {
    call.reset()
    review.resetReview()
  }

  const isFree = call.selectedSku?.free === true
  const inQuoteFlow = agent.hasQuote && !isFree && ['quoted', 'paying', 'invoking', 'polling'].includes(call.status)
  const fieldsDisabled = call.busy || (agent.hasQuote === true && !isFree && call.status === 'quoted')
  const skuTon = call.selectedSku?.priceTon ?? agent.price
  const skuUsdt = call.selectedSku?.priceUsdt ?? agent.priceUsdt
  const skuSol = call.selectedSku?.priceSol ?? agent.priceSol
  const skuUsdc = call.selectedSku?.priceUsdc ?? agent.priceUsdc
  const payLabel = call.selectedRail === 'SOL' && skuSol ? formatRailPrice('SOL', skuSol)
    : call.selectedRail === 'USDC' && skuUsdc ? formatRailPrice('USDC', skuUsdc)
    : call.selectedRail === 'USDT' && skuUsdt ? formatRailPrice('USDT', skuUsdt)
    : formatRailPrice('TON', skuTon)
  // Each rail pays from its own chain's wallet.
  const solanaRail = isSolanaRail(call.selectedRail)
  const walletAddress = solanaRail ? (solAccount?.address ?? '') : tonAddress
  // Quotes price TON/USDT only (SOL/USDC quotes are refused by the sidecar).
  const quoteRails = call.paymentRails.filter(r => !isSolanaRail(r))
  const quoteUsdt = call.quote?.priceUsdt ?? skuUsdt
  const selectedSoldOut = call.selectedSku?.stockLeft != null && call.selectedSku.stockLeft <= 0
  const submitDisabled = call.busy || online === false || selectedSoldOut

  return (
    <div className={`agent-item ${expanded ? 'agent-item--open' : ''}${locked ? ' agent-item--locked' : ''}`}>
      {/* Row — always visible */}
      <button className="agent-row" onClick={locked ? undefined : onToggle} aria-expanded={expanded} disabled={locked}>
        {(agent.previewUrl || agent.avatarUrl) && <AgentThumb url={agent.previewUrl || agent.avatarUrl!} />}
        <div className="agent-row-left">
          {!locked && <span className="agent-row-name">{agent.name || agent.address.slice(0, 10) + '…'}</span>}
          <div className="agent-row-meta">
            {online === true && !pinging && <span className="tag" style={{color: 'var(--success)', borderColor: 'var(--success)'}}>online</span>}
            {online === false && !pinging && <span className="agent-offline-badge">offline</span>}
            {pinging && <span className="tag">…</span>}
            {agent.chains?.includes('solana') && <span className="tag chain-tag">solana</span>}
            {agent.capabilities.map(c => <span key={c} className="tag">{c}</span>)}
          </div>
        </div>
        <div className="agent-row-right">
          <span className="agent-row-price">{agent.hasQuote && 'from '}<PriceBadge agent={agent} /></span>
          <span className={`chevron ${expanded ? 'chevron--open' : ''}`}>
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M4 6l4 4 4-4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </span>
        </div>
      </button>

      {/* Summary — always visible */}
      {(onChainRating || ratingLoading || ratingError || agent.description) && (
        <div className="agent-summary" onClick={locked ? undefined : onToggle}>
          <RatingBlock rating={onChainRating} loading={ratingLoading} error={ratingError} onRefresh={ratingRefresh} />
          {agent.description && <AgentDesc description={agent.description} full={!!locked || expanded} />}
        </div>
      )}

      {/* Expanded body */}
      {expanded && (
        <div className="agent-body">
          {agent.images && agent.images.length > 0 && <AgentGallery images={agent.images} />}
          <div className="agent-body-meta">
            <div className="meta-item">
              <span className="meta-label">Endpoint</span>
              <a href={agent.endpoint} target="_blank" rel="noopener noreferrer" className="link">{agent.endpoint}</a>
              <ConnectionBadge mode={call.connMode} />
            </div>
            {agent.address && <div className="meta-item">
              <span className="meta-label">Wallet</span>
              <a
                href={`https://${TESTNET ? 'testnet.' : ''}tonviewer.com/${friendlyAddr(agent.address)}`}
                target="_blank" rel="noopener noreferrer" className="link meta-addr"
              >
                {formatAddr(agent.address)}
              </a>
              <CopyButton text={friendlyAddr(agent.address)} />
            </div>}
            <div className="meta-item">
              <span className="meta-label">Share</span>
              <ShareButton sidecarId={agent.sidecarId} />
            </div>
          </div>

          <div className="agent-divider" />

          {!walletAddress ? (
            <div className="call-form">
              <RailSelector
                rails={call.paymentRails}
                selected={call.selectedRail}
                onSelect={call.setSelectedRail}
                disabled={call.busy}
              />
              <div className="alert alert-info">
                {solanaRail
                  ? 'Connect a Solana wallet (Phantom, Solflare, Backpack) to pay in SOL or USDC.'
                  : 'Connect your wallet to call this agent.'}
              </div>
            </div>
          ) : call.status === 'refunded' ? (
            <RefundedBlock
              reason={call.refundReason}
              reasonCode={call.refundReasonCode}
              refundTx={call.refundTx}
              onReset={handleReset}
            />
          ) : call.status === 'done' && call.result ? (
            <div className="result-box">
              <span className="meta-label">Result</span>
              <ResultRenderer result={call.result} downloadUrl={(path) => resolveDownloadUrl(agent.endpoint, path)} />

              {!call.lastNonce ? null : review.reviewStatus === 'sent' ? (
                <div className="review-done">
                  <span className="review-done-icon">✓</span>
                  <span>Thanks! Your rating is on its way on-chain.</span>
                </div>
              ) : (
                <div className="review-cta">
                  <p className="review-cta-text">Enjoyed the result? Rate this agent to help others discover quality services.</p>
                  <div className="review-stars">
                    {[1, 2, 3, 4, 5].map(s => (
                      <button key={s} type="button"
                        className={`review-star ${s <= (review.reviewHover || review.reviewScore) ? 'review-star--active' : ''}`}
                        onMouseEnter={() => review.setReviewHover(s)}
                        onMouseLeave={() => review.setReviewHover(0)}
                        onClick={() => review.setReviewScore(s)}
                        disabled={review.reviewStatus === 'sending'}>
                        ★
                      </button>
                    ))}
                  </div>
                  {review.reviewScore > 0 && (
                    <button className="btn btn-review" onClick={review.handleReview} disabled={review.reviewStatus === 'sending'}>
                      {review.reviewStatus === 'sending' ? 'Submitting…' : 'Submit Rating · 0.01 TON'}
                    </button>
                  )}
                  {review.reviewStatus === 'error' && (
                    <p className="review-error">Failed to submit. Try again?</p>
                  )}
                </div>
              )}

              <button className="btn btn-outline btn-sm" onClick={handleReset}>Call again</button>
            </div>
          ) : (
            <form
              onSubmit={agent.hasQuote && !isFree && call.status !== 'quoted' ? call.handleGetQuote : call.handleSubmit}
              className="call-form"
            >
              {call.errorMsg && <div className="alert alert-error">{call.errorMsg}</div>}

              {call.skus.length > 1 && (
                <SkuSelector
                  skus={call.skus}
                  selectedId={call.selectedSkuId}
                  onSelect={call.setSelectedSkuId}
                  disabled={fieldsDisabled}
                />
              )}
              {call.skus.length === 1 && call.skus[0] && (
                <StockBadge sku={call.skus[0]} />
              )}

              <InputFields
                schema={agent.argsSchema}
                fields={call.fields}
                setFields={call.setFields}
                setFileFields={call.setFileFields}
                disabled={fieldsDisabled}
              />

              {agent.hasQuote && call.status === 'quoted' && call.quote && (
                <div className="quote-box">
                  {call.quote.plan && typeof call.quote.plan === 'object' && 'steps' in call.quote.plan && (
                    <div className="quote-plan">
                      {call.quote.plan.steps.map((s, i) => (
                        <div key={i} className="quote-step">
                          <span className="quote-step-num">{s.step + 1}</span>
                          <span className="quote-step-agent">{s.agent}</span>
                          <span className="quote-step-cap">{s.capability}</span>
                          <span className="quote-step-price">{s.price_ton}</span>
                        </div>
                      ))}
                    </div>
                  )}
                  {call.quote.plan && typeof call.quote.plan === 'string' && call.quote.plan && (
                    <div className="quote-plan">{call.quote.plan}</div>
                  )}
                  {call.quote.note && (
                    <div className="quote-note">{call.quote.note}</div>
                  )}
                  <div className="quote-meta">
                    <span className="quote-price">{nanoToTon(call.quote.price)} TON{quoteUsdt ? ` / ${microToUsdt(quoteUsdt)} USDT` : ''}</span>
                    <span className={`quote-timer ${call.quoteSecondsLeft === 0 ? 'quote-timer--expired' : ''}`}>
                      {call.quoteSecondsLeft > 0 ? `Expires in ${call.quoteSecondsLeft}s` : 'Quote expired'}
                    </span>
                  </div>
                </div>
              )}

              {online === false && (
                <div className="alert alert-warn">
                  <span>Agent appears to be offline. Sending payment may result in a loss of funds.</span>
                  <button type="button" className="btn btn-outline btn-sm" onClick={recheck} disabled={pinging}>
                    {pinging ? 'Checking…' : 'Check again'}
                  </button>
                </div>
              )}

              {isFree ? (
                <button type="submit" className="btn btn-primary" disabled={submitDisabled}>
                  {call.status === 'invoking' ? 'Calling agent…'
                    : call.status === 'polling' ? 'Waiting for result…'
                    : selectedSoldOut ? 'Sold out'
                    : 'Get for free'}
                </button>
              ) : inQuoteFlow ? (
                <div className="quote-actions">
                  <RailSelector
                    rails={quoteRails}
                    selected={call.selectedRail}
                    onSelect={call.setSelectedRail}
                    disabled={call.busy}
                  />
                  <button type="submit" className="btn btn-primary" disabled={submitDisabled || call.quoteSecondsLeft === 0}>
                    {call.status === 'paying' ? 'Waiting for payment…'
                      : call.status === 'invoking' ? 'Calling agent…'
                      : call.status === 'polling' ? 'Waiting for result…'
                      : call.quoteSecondsLeft === 0 ? 'Quote expired'
                      : selectedSoldOut ? 'Sold out'
                      : call.selectedRail === 'USDT' && quoteUsdt
                        ? `Approve & Pay ${microToUsdt(quoteUsdt)} USDT`
                        : `Approve & Pay ${nanoToTon(call.quote!.price)} TON`}
                  </button>
                  <button type="button" className="btn btn-outline btn-sm" onClick={() => call.resetQuote()}>
                    Get new quote
                  </button>
                </div>
              ) : agent.hasQuote ? (
                <button type="submit" className="btn btn-primary" disabled={submitDisabled}>
                  {call.status === 'quoting' ? 'Getting quote…'
                    : selectedSoldOut ? 'Sold out'
                    : 'Get Quote'}
                </button>
              ) : (
                <>
                  <RailSelector
                    rails={call.paymentRails}
                    selected={call.selectedRail}
                    onSelect={call.setSelectedRail}
                    disabled={call.busy}
                  />
                  <button type="submit" className="btn btn-primary" disabled={submitDisabled}>
                    {call.status === 'paying' ? 'Waiting for payment…'
                      : call.status === 'invoking' ? 'Calling agent…'
                      : call.status === 'polling' ? 'Waiting for result…'
                      : selectedSoldOut ? 'Sold out'
                      : `Pay ${payLabel} & Execute`}
                  </button>
                </>
              )}
            </form>
          )}
        </div>
      )}
    </div>
  )
}
