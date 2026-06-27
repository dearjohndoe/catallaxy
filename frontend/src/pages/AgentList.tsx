import { useEffect, useState } from 'react'
import { useStore } from '../store/useStore'
import { AgentItem } from '../components/AgentItem'

export function AgentList() {
  const { allAgents, visibleCount, loading, error, hasMoreOnChain, init, refresh, loadMore } = useStore()
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [spinning, setSpinning] = useState(false)

  useEffect(() => { init() }, [])

  async function handleRefresh() {
    if (loading || spinning) return
    setSpinning(true)
    await refresh()
    setSpinning(false)
  }

  const displayed = allAgents.slice(0, visibleCount)
  const canLoadMore = !loading && (visibleCount < allAgents.length || hasMoreOnChain)

  function toggle(id: string) {
    setExpandedId(prev => prev === id ? null : id)
  }

  return (
    <div className="page">
      <div className="hero">
        <h1><span className="hero-bracket">&gt; </span>agent_marketplace</h1>
        <p>decentralized AI agents on TON — discover, call or <a target="_blank" href="https://github.com/dearjohndoe/catallaxy/blob/master/sidecar/README.md" className="link-inline">add your own</a></p>
      </div>

      {error && <div className="alert alert-error">{error}</div>}

      {loading && displayed.length === 0 ? (
        <div className="state-msg">Loading agents…</div>
      ) : displayed.length === 0 ? (
        <div className="state-msg">No agents registered yet.</div>
      ) : (
        <>
          <div className="list-header">
            <span className="list-count">{allAgents.length} agent{allAgents.length !== 1 ? 's' : ''} found</span>
            <button
              className={`btn-refresh-list${spinning ? ' btn-refresh-list--spinning' : ''}`}
              onClick={handleRefresh}
              disabled={loading || spinning}
              title="Refresh agents"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                <polyline points="1 4 1 10 7 10" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"/>
                <polyline points="23 20 23 14 17 14" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"/>
                <path d="M20.49 9A9 9 0 0 0 5.64 5.64L1 10m22 4-4.64 4.36A9 9 0 0 1 3.51 15" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
              Refresh
            </button>
          </div>
          <div className="agent-list">
            {displayed.map(agent => (
              <AgentItem
                key={agent.sidecarId}
                agent={agent}
                expanded={expandedId === agent.sidecarId}
                onToggle={() => toggle(agent.sidecarId)}
              />
            ))}
          </div>

          {canLoadMore && (
            <div className="load-more-wrap">
              <button className="btn btn-outline" onClick={loadMore} disabled={loading}>
                {loading ? 'Loading…' : 'Load more'}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
