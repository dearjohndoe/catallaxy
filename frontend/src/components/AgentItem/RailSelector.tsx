const RAIL_LABELS: Record<string, string> = {
  TON: 'TON', USDT: 'USDT', SOL: 'SOL', USDC: 'USDC · Solana',
}

export function RailSelector({ rails, selected, onSelect, disabled, unavailable = [] }: {
  rails: string[]
  selected: string
  onSelect: (rail: string) => void
  disabled: boolean
  /** Rails the agent lists but can't take payments on right now. */
  unavailable?: string[]
}) {
  const down = rails.filter(r => unavailable.includes(r))
  if (rails.length < 2 && down.length === 0) return null
  return (
    <>
      <div className="rail-selector">
        {rails.map(rail => {
          const isDown = down.includes(rail)
          return (
            <label key={rail} className="rail-option"
              title={isDown ? 'Temporarily unavailable: the agent cannot see this network right now' : undefined}>
              <input type="radio" name="rail" value={rail}
                checked={selected === rail}
                onChange={() => onSelect(rail)}
                disabled={disabled || isDown} />
              <span>{RAIL_LABELS[rail] ?? rail}{isDown ? ' · offline' : ''}</span>
            </label>
          )
        })}
      </div>
      {down.length > 0 && (
        <div className="rail-unavailable-note">
          {down.map(r => RAIL_LABELS[r] ?? r).join(', ')} temporarily unavailable — try again in a minute
        </div>
      )}
    </>
  )
}
