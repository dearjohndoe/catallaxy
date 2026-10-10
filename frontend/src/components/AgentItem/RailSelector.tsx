const RAIL_LABELS: Record<string, string> = {
  TON: 'TON', USDT: 'USDT', SOL: 'SOL', USDC: 'USDC · Solana',
}

export function RailSelector({ rails, selected, onSelect, disabled }: {
  rails: string[]
  selected: string
  onSelect: (rail: string) => void
  disabled: boolean
}) {
  if (rails.length < 2) return null
  return (
    <div className="rail-selector">
      {rails.map(rail => (
        <label key={rail} className="rail-option">
          <input type="radio" name="rail" value={rail}
            checked={selected === rail}
            onChange={() => onSelect(rail)}
            disabled={disabled} />
          <span>{RAIL_LABELS[rail] ?? rail}</span>
        </label>
      ))}
    </div>
  )
}
