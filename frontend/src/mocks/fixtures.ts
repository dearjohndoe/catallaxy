import type { SidecarFixture } from './backend'

/**
 * Mock sidecar fixtures. Each entry is a self-contained agent with its own
 * stock, behavior, and (optionally) dynamic quote pricing.
 *
 * Endpoints must be unique — the backend keys state by URL origin.
 */
export const FIXTURES: SidecarFixture[] = [
  // ── 1. Single SKU, finite stock, happy path ──────────────────────
  {
    sidecarId: 'mock-stock-basic',
    endpoint: 'https://stock-basic.mock.local',
    paymentRails: ['TON'],
    agent: {
      address: 'EQBvW8Z5huBkMJYdnfAEM5JqTNkuWX3diqYENkWsIL0XggGG',
      name: 'Stock Demo · Single SKU',
      description: 'Sells 10 widgets, decrements stock on every successful sale.',
      capabilities: ['sell_widget', 'widgets.premium'],
      argsSchema: {
        recipient: { type: 'string', description: 'Where to deliver', required: true },
      },
      previewUrl: 'https://picsum.photos/seed/stock1/200',
    },
    skus: [
      { id: 'default', title: 'Standard Widget', priceTon: 100_000_000, initialStock: 10 },
    ],
    behavior: ({ body }) => ({
      kind: 'success',
      delayMs: 400,
      result: { type: 'string', data: `Widget delivered to ${body?.recipient ?? '???'}` },
    }),
  },

  // ── 2. Multi-SKU agent ──────────────────────────────────────────
  {
    sidecarId: 'mock-stock-multi',
    endpoint: 'https://stock-multi.mock.local',
    paymentRails: ['TON', 'USDT'],
    agent: {
      address: 'EQAREREREREREREREREREREREREREREREREREREREREREeYT',
      name: 'Stock Demo · Multi SKU',
      description: 'Three tiers of accounts: basic (10), pro (3), elite (1).',
      capabilities: ['sell_account'],
      argsSchema: {
        contact: { type: 'string', description: 'Telegram handle for delivery', required: true },
      },
      hasQuote: false,
      previewUrl: 'https://picsum.photos/seed/stock-multi/200',
    },
    skus: [
      { id: 'basic', title: 'Basic account · lvl 10', priceTon: 50_000_000, priceUsdt: 100_000, initialStock: 10 },
      { id: 'pro', title: 'Pro account · lvl 50', priceTon: 500_000_000, priceUsdt: 1_000_000, initialStock: 3 },
      { id: 'elite', title: 'Elite account · lvl 99', priceTon: 2_000_000_000, priceUsdt: 5_000_000, initialStock: 1 },
    ],
    behavior: ({ skuId, body }) => ({
      kind: 'success',
      delayMs: 600,
      result: {
        type: 'string',
        data: `Account [${skuId}] credentials sent to ${body?.contact ?? 'unknown'} via DM.`,
      },
    }),
  },

  // ── 3. Agent always reports out_of_stock ────────────────────────
  {
    sidecarId: 'mock-stock-oos',
    endpoint: 'https://stock-oos.mock.local',
    paymentRails: ['TON'],
    agent: {
      address: 'EQAiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIp3C',
      name: 'Stock Demo · Agent OOS',
      description: 'Always reports out_of_stock — exercises the refund flow.',
      capabilities: ['unlucky_sell'],
      argsSchema: {},
    },
    skus: [
      { id: 'default', title: 'Cursed Item', priceTon: 25_000_000, initialStock: 5 },
    ],
    behavior: () => ({
      kind: 'out_of_stock',
      delayMs: 800,
      reason: 'Last unit was claimed seconds before delivery.',
    }),
  },

  // ── 4. Already sold out at startup ──────────────────────────────
  {
    sidecarId: 'mock-stock-sold-out',
    endpoint: 'https://stock-soldout.mock.local',
    paymentRails: ['TON'],
    agent: {
      address: 'EQAzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzM7SN',
      name: 'Stock Demo · Sold Out',
      description: 'Stock reads zero from the start — preflight returns 409.',
      capabilities: ['nope'],
      argsSchema: {},
    },
    skus: [{ id: 'default', title: 'Empty', priceTon: 10_000_000, initialStock: 0 }],
    behavior: () => ({ kind: 'success', result: { type: 'string', data: 'unreachable' } }),
  },

  // ── 5. Quote-based pricing + multi-SKU ──────────────────────────
  {
    sidecarId: 'mock-stock-quote',
    endpoint: 'https://stock-quote.mock.local',
    paymentRails: ['TON'],
    agent: {
      address: 'EQA0i8-CdGnF_DhUHHf92R1ONH6sIA9vLZ_WLcCIhfBBXwtG',
      name: 'Stock Demo · Quote Flow',
      description: 'Two SKUs, dynamic quote price based on input length.',
      capabilities: ['render'],
      hasQuote: true,
      argsSchema: {
        text: { type: 'string', description: 'Source text', required: true },
      },
    },
    skus: [
      { id: 'small', title: 'Small render', priceTon: 50_000_000, initialStock: 20 },
      { id: 'big', title: 'Big render', priceTon: 200_000_000, initialStock: 5 },
    ],
    quotePrice: ({ skuId, body }) => {
      const len = String(body?.text ?? '').length
      const base = skuId === 'big' ? 200_000_000 : 50_000_000
      return { price: base + len * 1_000_000, ttl: 60_000 }
    },
    behavior: ({ skuId }) => ({
      kind: 'success',
      delayMs: 1000,
      result: { type: 'string', data: `Rendered (${skuId}) ✓` },
    }),
  },

  // ── 6. Agent errors out (release reservation, no decrement) ─────
  {
    sidecarId: 'mock-stock-flaky',
    endpoint: 'https://stock-flaky.mock.local',
    paymentRails: ['TON'],
    agent: {
      address: 'EQBvW8Z5huBkMJYdnfAEM5JqTNkuWX3diqYENkWsIL0XggGG',
      name: 'Stock Demo · Flaky',
      description: 'Returns error half the time — reservation is released, total untouched.',
      capabilities: ['flaky'],
      argsSchema: {},
    },
    skus: [{ id: 'default', title: 'Flaky', priceTon: 30_000_000, initialStock: 50 }],
    behavior: () =>
      Math.random() < 0.5
        ? { kind: 'error', message: 'Internal agent failure', delayMs: 300 }
        : { kind: 'success', delayMs: 300, result: { type: 'string', data: 'lucky!' } },
  },
]
