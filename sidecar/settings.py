import enum
import hashlib
import os
import re
from dataclasses import dataclass

from chains.ton.jetton import USDT_MASTER_MAINNET, USDT_MASTER_TESTNET


DEFAULT_SKU_ID = "default"


class SkuKind(str, enum.Enum):
    """Pricing model of a SKU.

    STATIC  — fixed price per rail (price_ton/price_usd hold the amount).
    DYNAMIC — price resolved at runtime via the agent's `mode=prices`. Stored
              internally as price 0 (the legacy sentinel) so all existing
              `price == 0` dynamic checks keep working unchanged.
    FREE    — no on-chain payment; both prices are None, no rails.
    """

    STATIC = "static"
    DYNAMIC = "dynamic"
    FREE = "free"

REGISTRY_ADDRESS = "UQCYxSFNCJHmBxVpgfqAesgjLQDsLch3WJG3MJYyhnBDS7gg"


def _agent_slug(agent_name: str) -> str:
    """Filesystem-safe identifier derived from AGENT_NAME.

    Used to namespace per-agent state files so two sidecars on the same host
    never collide on stock.db / processed_txs.db. ASCII-only; non-ASCII names
    fall back to an md5 prefix so we never produce an empty slug.
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", agent_name).strip("-").lower()
    if not slug:
        slug = "agent-" + hashlib.md5(agent_name.encode("utf-8")).hexdigest()[:8]
    return slug


@dataclass(frozen=True)
class AgentSku:
    sku_id: str
    title: str
    price_ton: int | None   # nanoton, None if TON rail not supported
    price_usd: int | None   # micro-USD, None if USDT rail not supported
    initial_stock: int | None  # None => infinite (no stock tracking)
    kind: SkuKind = SkuKind.STATIC
    price_usdc: int | None = None  # micro-USDC on Solana; None if rail off
    price_sol: int | None = None   # lamports (native SOL); None if rail off

    # rail_id -> price accessors over the price_ton/price_usd backing fields, so
    # rail-agnostic code needn't branch on "TON"/"USDT".
    def price_for(self, rail_id: str) -> int | None:
        """Price on ``rail_id`` (nanoton for TON, micro-USD for USDT / USDC,
        lamports for SOL), or None
        if the rail isn't priced. A price of 0 is the dynamic-pricing sentinel."""
        return {
            "TON": self.price_ton,
            "USDT": self.price_usd,
            "USDC": self.price_usdc,
            "SOL": self.price_sol,
        }.get(rail_id)

    @property
    def prices(self) -> dict[str, int]:
        """rail_id -> price for every priced rail (unpriced rails omitted)."""
        return {
            r: p for r, p in (
                ("TON", self.price_ton),
                ("USDT", self.price_usd),
                ("USDC", self.price_usdc),
                ("SOL", self.price_sol),
            )
            if p is not None
        }


@dataclass
class Settings:
    agent_command: str
    capabilities: tuple[str, ...]
    agent_name: str
    agent_description: str
    agent_price: int
    agent_endpoint: str
    # TON key/wallet: None on a Solana-only agent (no TON/USDT rails).
    agent_wallet_pk: str | None
    agent_wallet_seed: str | None
    agent_wallet: str | None
    registry_address: str
    port: int
    payment_timeout: int
    sync_timeout: int
    final_timeout: int
    jobs_ttl: int
    testnet: bool
    state_path: str
    tx_db_path: str
    stock_db_path: str
    enforce_comment_nonce: bool
    refund_fee_nanoton: int
    refund_worker_interval: int
    refund_max_attempts: int
    agent_price_usdt: int | None
    has_quote: bool
    rate_limit_requests: int
    rate_limit_window: int
    trusted_proxy_ips: frozenset[str]
    file_store_dir: str
    file_store_ttl: int
    images_dir: str
    agent_preview_url: str | None
    agent_avatar_url: str | None
    agent_images: tuple[str, ...]
    owner_wallet: str | None
    skus: tuple[AgentSku, ...]
    payment_rails: tuple[str, ...]
    tg_bot_token: str | None
    tg_user_ids: tuple[int, ...]
    # Free-SKU abuse limit: max claims per client IP within the rolling window.
    free_claim_limit: int = 1
    free_claim_window_seconds: int = 2592000  # 30 days
    # Jetton master for the USDT rail. Default is Tether (mainnet or official
    # testnet USD). Override with TON_USDT_MASTER / USDT_MASTER for a custom
    # TEP-74 minter — then also set TON_JETTON_WALLET_CODE.
    usdt_master: str = USDT_MASTER_MAINNET
    jetton_wallet_code_hex: str | None = None
    sol_wallet_pk: str | None = None
    sol_wallet: str | None = None
    # Solana half of OWNER_WALLET; owner_wallet keeps the TON half.
    owner_wallet_sol: str | None = None
    sol_rpc_url: str | None = None
    sol_cluster: str = "mainnet"
    sol_usdc_mint: str | None = None
    agent_price_usdc: int | None = None
    agent_price_sol: int | None = None
    # Native SOL rail: lamports kept from each refund (buyer pays the tx fee,
    # like refund_fee_nanoton on TON) and the commitment we deliver on.
    sol_refund_fee_lamports: int = 5_000
    # USDC rail: micro-USDC kept from each refund, and extra when the refund
    # has to recreate the buyer's USDC account (rent paid by the seller).
    sol_usdc_refund_fee: int = 5_000
    sol_usdc_refund_ata_fee: int = 200_000
    # USDC settle throttles (the seller pays SOL for settles that fail
    # on-chain): cooldown per buyer token account, cap per client IP.
    sol_usdc_source_cooldown_seconds: int = 60
    sol_usdc_ip_max_failed_settles: int = 5
    sol_usdc_ip_failure_window_seconds: int = 600
    sol_commitment: str = "confirmed"
    # Solana discovery registry wallet. None → no Solana heartbeat (mainnet has
    # no default yet).
    sol_registry_address: str | None = None

    def owner_wallets(self) -> dict[str, str]:
        out = {}
        if self.owner_wallet:
            out["ton"] = self.owner_wallet
        if self.owner_wallet_sol:
            out["sol"] = self.owner_wallet_sol
        return out

    def payment_addresses(self) -> dict[str, str]:
        out = {}
        if self.agent_wallet:
            out["ton"] = self.agent_wallet
        if self.sol_wallet:
            out["sol"] = self.sol_wallet
        return out

    @property
    def capability(self) -> str:
        """Primary capability — first in the declared list."""
        return self.capabilities[0]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _chain_env(chain: str, suffix: str, *legacy: str, default: str | None = None) -> str | None:
    """Read a chain-scoped env var, preferring the per-chain name
    ``{CHAIN}_{SUFFIX}`` (e.g. ``TON_WALLET_PK``) and falling back to the legacy
    unprefixed names. Returns ``default`` if none are set."""
    val = os.getenv(f"{chain.upper()}_{suffix}")
    if val is not None:
        return val
    for name in legacy:
        v = os.getenv(name)
        if v is not None:
            return v
    return default


def _chain_env_bool(chain: str, suffix: str, *legacy: str, default: bool = False) -> bool:
    raw = _chain_env(chain, suffix, *legacy)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


OWNER_WALLET_SEPARATOR = ","


def parse_owner_wallet(raw: str | None) -> tuple[str | None, str | None]:
    """OWNER_WALLET = ``<ton>[,<sol>]``: ``UQ…``, ``UQ…,``, ``,<sol>`` or
    ``UQ…,<sol>``. Comma, because raw TON addresses (``0:hex``) contain a
    colon and standard base64 TON addresses may contain ``/`` and ``+``."""
    s = (raw or "").strip()
    if not s:
        return None, None
    parts = s.split(OWNER_WALLET_SEPARATOR)
    if len(parts) > 2:
        raise RuntimeError("OWNER_WALLET: expected '<ton>', '<ton>,<sol>' or ',<sol>'")
    ton = parts[0].strip() or None
    sol = parts[1].strip() if len(parts) == 2 else ""
    sol = sol or None
    if ton is None and sol is None:
        raise RuntimeError("OWNER_WALLET: no address given")
    if ton is not None:
        try:
            from pytoniq_core import Address
            Address(ton)
        except Exception as exc:
            raise RuntimeError(f"OWNER_WALLET: invalid TON address '{ton}'") from exc
    if sol is not None:
        try:
            from solders.pubkey import Pubkey
            Pubkey.from_string(sol)
        except Exception as exc:
            raise RuntimeError(f"OWNER_WALLET: invalid Solana address '{sol}'") from exc
    return ton, sol


def _parse_capabilities(raw: str) -> tuple[str, ...]:
    """AGENT_CAPABILITY accepts a comma-separated list; first entry is primary."""
    caps = tuple(dict.fromkeys(c.strip() for c in raw.split(",") if c.strip()))
    if not caps:
        raise RuntimeError("AGENT_CAPABILITY must contain at least one capability")
    return caps


def _derive_wallet_address(pk_hex: str, testnet: bool) -> str:
    from tonutils.contracts.wallet import WalletV4R2
    from tonutils.types import PrivateKey
    pk = PrivateKey(bytes.fromhex(pk_hex.removeprefix("0x")))
    wallet = WalletV4R2.from_private_key(None, pk)  # type: ignore[arg-type]
    return wallet.address.to_str(
        is_user_friendly=True,
        is_bounceable=False,
        is_url_safe=True,
        is_test_only=testnet,
    )


def _parse_sku_prices(
    price_spec: list[str], sku_id: str,
) -> tuple[int | None, int | None, int | None, int | None, SkuKind]:
    """Parse price tokens like ['ton=1000000000', 'usd=1500000'].

    Special forms:
    - ['free']                          -> FREE SKU (no rails, no on-chain payment)
    - 'ton=0'/'usd=0' (legacy) or 'ton=dynamic'/'usd=dynamic' -> dynamic pricing
      on that rail; the price is resolved at runtime via the agent's mode=prices.
      Dynamic is stored internally as price 0 for backward compatibility, so
      'ton=dynamic' is exactly equivalent to the legacy 'ton=0'.

    At least one price token is required unless the SKU is 'free'.
    """
    tokens = [t.strip() for t in price_spec if t.strip()]

    if any(t.lower() == "free" for t in tokens):
        if len(tokens) != 1:
            raise RuntimeError(f"SKU '{sku_id}': 'free' cannot be combined with other price tokens")
        return None, None, None, None, SkuKind.FREE

    price_ton: int | None = None
    price_usd: int | None = None
    price_usdc: int | None = None
    price_sol: int | None = None
    for token in tokens:
        if "=" not in token:
            raise RuntimeError(f"SKU '{sku_id}': invalid price token '{token}' (expected ton=<n> or usd=<n>)")
        key, _, val = token.partition("=")
        key = key.strip().lower()
        val = val.strip()
        if not val:
            raise RuntimeError(f"SKU '{sku_id}': empty value for '{key}'")
        if val.lower() == "dynamic":
            ival = 0  # dynamic sentinel — stored as 0 for backward compatibility
        else:
            try:
                ival = int(val)
            except ValueError:
                raise RuntimeError(f"SKU '{sku_id}': '{key}' must be an integer or 'dynamic', got '{val}'")
            if ival < 0:
                raise RuntimeError(f"SKU '{sku_id}': '{key}' must be >= 0 (use 0 or 'dynamic' for dynamic pricing)")
        if key == "ton":
            price_ton = ival
        elif key == "usd":
            price_usd = ival
        elif key == "usdc":
            if ival == 0:
                raise RuntimeError(f"SKU '{sku_id}': usdc dynamic pricing is not supported")
            price_usdc = ival
        elif key == "sol":
            if ival == 0:
                raise RuntimeError(f"SKU '{sku_id}': sol dynamic pricing is not supported")
            price_sol = ival
        else:
            raise RuntimeError(f"SKU '{sku_id}': unknown price key '{key}' (use ton, usd, usdc, or sol)")

    if price_ton is None and price_usd is None and price_usdc is None and price_sol is None:
        raise RuntimeError(f"SKU '{sku_id}': at least one of ton/usd/usdc/sol price required")

    # Any priced rail at 0 is the dynamic-pricing sentinel: that rail's price is
    # resolved at runtime via mode=prices. A SKU may mix a fixed rail with a
    # dynamic one (e.g. fixed USDT + floating TON anchored to a fiat price), so
    # one rail == 0 is enough to make the SKU dynamic. Both-rails-0 (legacy
    # rub_anchored) is just the all-dynamic special case of this rule.
    kind = SkuKind.DYNAMIC if price_ton == 0 or price_usd == 0 else SkuKind.STATIC

    return price_ton, price_usd, price_usdc, price_sol, kind


def _parse_tg_user_ids(raw: str) -> tuple[int, ...]:
    """Parse TG_USER_ID_LIST='12345,67890'. Skips blanks; raises on non-int tokens."""
    ids: list[int] = []
    seen: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            uid = int(chunk)
        except ValueError:
            raise RuntimeError(f"TG_USER_ID_LIST: invalid id '{chunk}' (must be integer)")
        if uid in seen:
            continue
        seen.add(uid)
        ids.append(uid)
    return tuple(ids)


def _parse_sku_titles(raw: str) -> dict[str, str]:
    """Parse AGENT_SKU_TITLES='basic=Basic account,premium=Premium lvl 50'."""
    result: dict[str, str] = {}
    if not raw:
        return result
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            continue
        sku_id, _, title = chunk.partition("=")
        sku_id = sku_id.strip()
        title = title.strip()
        if sku_id and title:
            result[sku_id] = title
    return result


def _parse_skus(raw_skus: str, raw_titles: str) -> tuple[AgentSku, ...]:
    """Parse AGENT_SKUS. Format: 'sku:stock:ton=N:usd=M,sku2:stock:ton=N'."""
    titles = _parse_sku_titles(raw_titles)
    skus: list[AgentSku] = []
    seen: set[str] = set()

    for entry in raw_skus.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) < 3:
            raise RuntimeError(f"Invalid SKU entry '{entry}' (need sku:stock:price_spec)")
        sku_id = parts[0].strip()
        if not sku_id:
            raise RuntimeError(f"Invalid SKU entry '{entry}': empty id")
        if sku_id in seen:
            raise RuntimeError(f"Duplicate SKU id '{sku_id}'")
        seen.add(sku_id)

        stock_raw = parts[1].strip().lower()
        if stock_raw in {"", "infinite", "inf", "none"}:
            initial_stock: int | None = None
        else:
            try:
                initial_stock = int(stock_raw)
            except ValueError:
                raise RuntimeError(f"SKU '{sku_id}': invalid stock '{stock_raw}'")
            if initial_stock < 0:
                raise RuntimeError(f"SKU '{sku_id}': stock must be >= 0")

        price_ton, price_usd, price_usdc, price_sol, kind = _parse_sku_prices(parts[2:], sku_id)
        title = titles.get(sku_id) or sku_id
        skus.append(AgentSku(
            sku_id=sku_id, title=title,
            price_ton=price_ton, price_usd=price_usd,
            initial_stock=initial_stock, kind=kind,
            price_usdc=price_usdc, price_sol=price_sol,
        ))

    if not skus:
        raise RuntimeError("AGENT_SKUS is set but no valid SKU entries parsed")

    # All paid SKUs must share the same rail set. FREE SKUs have no rails and are
    # excluded so they can coexist with paid SKUs.
    rail_sets = {
        tuple(sorted([r for r, v in (("TON", s.price_ton), ("USD", s.price_usd), ("USDC", s.price_usdc), ("SOL", s.price_sol)) if v is not None]))
        for s in skus if s.kind is not SkuKind.FREE
    }
    if len(rail_sets) > 1:
        raise RuntimeError(f"inconsistent_sku_rails: all SKUs must support the same set of rails, got {rail_sets}")

    return tuple(skus)


def _synthesize_default_sku(agent_price: int, agent_price_usdt: int | None, stock_raw: str | None) -> AgentSku:
    """Build a single default SKU from legacy AGENT_PRICE/AGENT_PRICE_USD/AGENT_STOCK."""
    price_ton = agent_price if agent_price > 0 else None
    price_usd = agent_price_usdt if agent_price_usdt else None

    initial_stock: int | None = None
    if stock_raw is not None and stock_raw.strip().lower() not in {"", "infinite", "inf", "none"}:
        try:
            initial_stock = int(stock_raw)
        except ValueError:
            raise RuntimeError(f"AGENT_STOCK: invalid value '{stock_raw}'")
        if initial_stock < 0:
            raise RuntimeError("AGENT_STOCK must be >= 0")

    return AgentSku(
        sku_id=DEFAULT_SKU_ID, title=DEFAULT_SKU_ID,
        price_ton=price_ton, price_usd=price_usd,
        initial_stock=initial_stock,
    )


def load_settings(env_file: str | None = None) -> Settings:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=env_file)

    required_keys = [
        "AGENT_COMMAND",
        "AGENT_CAPABILITY",
        "AGENT_NAME",
        "AGENT_DESCRIPTION",
        "AGENT_ENDPOINT",
    ]
    missing = [key for key in required_keys if not os.getenv(key)]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

    if not os.getenv("AGENT_PRICE") and not os.getenv("AGENT_PRICE_USD") and not os.getenv("AGENT_SKUS"):
        raise RuntimeError("Either AGENT_SKUS or AGENT_PRICE/AGENT_PRICE_USD must be set")

    testnet = _chain_env_bool("TON", "TESTNET", "TESTNET", default=False)

    raw_skus = os.getenv("AGENT_SKUS", "").strip()
    if raw_skus:
        # AGENT_SKUS is the source of truth — AGENT_PRICE/AGENT_PRICE_USD are ignored.
        skus = _parse_skus(raw_skus, os.getenv("AGENT_SKU_TITLES", ""))
    else:
        legacy_price = int(os.getenv("AGENT_PRICE", "0"))
        legacy_price_usdt = int(os.environ["AGENT_PRICE_USD"]) if os.getenv("AGENT_PRICE_USD") else None
        skus = (_synthesize_default_sku(legacy_price, legacy_price_usdt, os.getenv("AGENT_STOCK")),)

    # Settings.agent_price / agent_price_usdt are derived from SKUs: min non-zero price
    # per rail. Zero is the dynamic-pricing sentinel — exclude from min, but if every
    # SKU on a rail is zero, propagate zero (keeps the rail enabled without a static price).
    # Rails are defined by the paid SKUs; FREE SKUs (no rails) are skipped, and a fully
    # free agent ends up with no rails and agent_price 0.
    base = next((s for s in skus if s.kind is not SkuKind.FREE), None)
    if base is None or base.price_ton is None:
        agent_price = 0
    else:
        non_zero = [s.price_ton for s in skus if s.kind is not SkuKind.FREE and s.price_ton]
        agent_price = min(non_zero) if non_zero else 0
    if base is None or base.price_usd is None:
        agent_price_usdt: int | None = None
    else:
        non_zero_usd = [s.price_usd for s in skus if s.kind is not SkuKind.FREE and s.price_usd]
        agent_price_usdt = min(non_zero_usd) if non_zero_usd else 0

    rails: list[str] = []
    if base is not None and base.price_ton is not None:
        rails.append("TON")
    if base is not None and base.price_usd is not None:
        rails.append("USDT")
    agent_price_usdc: int | None = None
    if base is not None and base.price_usdc is not None:
        rails.append("USDC")
        non_zero_usdc = [s.price_usdc for s in skus if s.kind is not SkuKind.FREE and s.price_usdc]
        agent_price_usdc = min(non_zero_usdc) if non_zero_usdc else base.price_usdc
    elif not raw_skus and os.getenv("AGENT_PRICE_USDC"):
        agent_price_usdc = int(os.environ["AGENT_PRICE_USDC"])
        skus = tuple(
            AgentSku(
                sku_id=s.sku_id, title=s.title, price_ton=s.price_ton,
                price_usd=s.price_usd, initial_stock=s.initial_stock,
                kind=s.kind, price_usdc=agent_price_usdc,
            )
            for s in skus
        )
        rails.append("USDC")
    agent_price_sol: int | None = None
    if base is not None and base.price_sol is not None:
        rails.append("SOL")
        agent_price_sol = min(s.price_sol for s in skus if s.kind is not SkuKind.FREE and s.price_sol)

    # TON chain key: per-chain TON_WALLET_PK, legacy AGENT_WALLET_PK. Needed for
    # the TON rails; otherwise optional (then no TON heartbeat either) as long
    # as there is a Solana key to heartbeat and refund with.
    agent_wallet_pk = _chain_env("TON", "WALLET_PK", "AGENT_WALLET_PK") or None
    ton_rails = [r for r in rails if r in ("TON", "USDT")]
    if not agent_wallet_pk and (ton_rails or not _chain_env("SOL", "WALLET_PK")):
        why = f"{'/'.join(ton_rails)} rail" if ton_rails else "an agent without SOL_WALLET_PK"
        raise RuntimeError(f"Missing required env vars: TON_WALLET_PK (or AGENT_WALLET_PK) — needed for {why}")

    sol_cluster_raw = (_chain_env("SOL", "CLUSTER") or "").strip().lower()
    if _chain_env_bool("SOL", "TESTNET", default=False) or sol_cluster_raw in {"devnet", "testnet"}:
        sol_cluster = "devnet"
    else:
        sol_cluster = "mainnet"
    from chains.solana.constants import (
        DEFAULT_RPC_DEVNET, DEFAULT_RPC_MAINNET, REGISTRY_ADDRESS_DEVNET, USDC_MINT_DEVNET, USDC_MINT_MAINNET,
    )
    sol_wallet_pk = _chain_env("SOL", "WALLET_PK")
    sol_wallet: str | None = None
    for sol_rail in ("USDC", "SOL"):
        if sol_rail in rails and not sol_wallet_pk:
            raise RuntimeError(f"{sol_rail} rail requires SOL_WALLET_PK")
    sol_commitment = (_chain_env("SOL", "COMMITMENT") or "confirmed").strip().lower()
    if sol_commitment not in {"confirmed", "finalized"}:
        raise RuntimeError("SOL_COMMITMENT must be confirmed or finalized")
    sol_refund_fee_lamports = int(_chain_env("SOL", "REFUND_FEE_LAMPORTS") or "5000")
    if sol_refund_fee_lamports < 0:
        raise RuntimeError("SOL_REFUND_FEE_LAMPORTS must be >= 0")
    sol_usdc_refund_fee = int(_chain_env("SOL", "USDC_REFUND_FEE") or "5000")
    sol_usdc_refund_ata_fee = int(_chain_env("SOL", "USDC_REFUND_ATA_FEE") or "200000")
    if sol_usdc_refund_fee < 0 or sol_usdc_refund_ata_fee < 0:
        raise RuntimeError("SOL_USDC_REFUND_FEE / SOL_USDC_REFUND_ATA_FEE must be >= 0")
    sol_usdc_source_cooldown_seconds = int(_chain_env("SOL", "USDC_SOURCE_COOLDOWN_SEC") or "60")
    sol_usdc_ip_max_failed_settles = int(_chain_env("SOL", "USDC_IP_MAX_FAILED_SETTLES") or "5")
    sol_usdc_ip_failure_window_seconds = int(_chain_env("SOL", "USDC_IP_FAILURE_WINDOW_SEC") or "600")
    if min(sol_usdc_source_cooldown_seconds, sol_usdc_ip_max_failed_settles,
           sol_usdc_ip_failure_window_seconds) < 0:
        raise RuntimeError("SOL_USDC_SOURCE_COOLDOWN_SEC / SOL_USDC_IP_* must be >= 0")
    if sol_wallet_pk:
        from chains.solana.wallet import parse_solana_keypair
        sol_wallet = str(parse_solana_keypair(sol_wallet_pk).pubkey())
    sol_rpc_url = _chain_env("SOL", "RPC_URL") or (
        DEFAULT_RPC_DEVNET if sol_cluster == "devnet" else DEFAULT_RPC_MAINNET
    )
    sol_usdc_mint = _chain_env("SOL", "USDC_MINT") or (
        USDC_MINT_DEVNET if sol_cluster == "devnet" else USDC_MINT_MAINNET
    )
    sol_registry_address = (_chain_env("SOL", "REGISTRY_ADDRESS") or "").strip() or (
        REGISTRY_ADDRESS_DEVNET if sol_cluster == "devnet" else None
    )
    if sol_registry_address:
        from solders.pubkey import Pubkey
        try:
            Pubkey.from_string(sol_registry_address)
        except ValueError:
            raise RuntimeError(f"SOL_REGISTRY_ADDRESS is not a valid Solana address: {sol_registry_address!r}")

    agent_name = os.environ["AGENT_NAME"]
    slug = _agent_slug(agent_name)

    # owner_bot: both TG_BOT_TOKEN and TG_USER_ID_LIST must be present and non-empty
    # for the bot to start. Either missing → bot stays off, no requests are made.
    tg_bot_token = (os.getenv("TG_BOT_TOKEN") or "").strip() or None
    tg_user_ids = _parse_tg_user_ids(os.getenv("TG_USER_ID_LIST", ""))
    if (tg_bot_token is None) != (not tg_user_ids):
        raise RuntimeError(
            "owner_bot: TG_BOT_TOKEN and TG_USER_ID_LIST must both be set, or both unset"
        )
    owner_ton, owner_sol = parse_owner_wallet(os.getenv("OWNER_WALLET"))
    return Settings(
        agent_command=os.environ["AGENT_COMMAND"],
        capabilities=_parse_capabilities(os.environ["AGENT_CAPABILITY"]),
        agent_name=agent_name,
        agent_description=os.environ["AGENT_DESCRIPTION"],
        agent_price=agent_price,
        agent_endpoint=os.environ["AGENT_ENDPOINT"],
        agent_wallet=_derive_wallet_address(agent_wallet_pk, testnet) if agent_wallet_pk else None,
        agent_wallet_pk=agent_wallet_pk,
        agent_wallet_seed=_chain_env("TON", "WALLET_SEED", "AGENT_WALLET_SEED"),
        registry_address=_chain_env("TON", "REGISTRY_ADDRESS", default=REGISTRY_ADDRESS),
        port=int(os.getenv("PORT", "8080")),
        payment_timeout=int(os.getenv("PAYMENT_TIMEOUT", "300")),
        sync_timeout=int(os.getenv("AGENT_SYNC_TIMEOUT", "30")),
        final_timeout=int(os.getenv("AGENT_FINAL_TIMEOUT", "1200")),
        jobs_ttl=int(os.getenv("JOBS_TTL_SECONDS", "3600")),
        testnet=testnet,
        state_path=os.getenv("SIDECAR_STATE_PATH", f".sidecar_state.{slug}.json"),
        tx_db_path=f"processed_txs.{slug}.db",
        stock_db_path=f"stock.{slug}.db",
        enforce_comment_nonce=_env_bool("ENFORCE_COMMENT_NONCE", True),
        refund_fee_nanoton=int(_chain_env("TON", "REFUND_FEE_NANOTON", "REFUND_FEE_NANOTON", default="500000")),
        refund_worker_interval=int(os.getenv("REFUND_WORKER_INTERVAL_SECONDS", "60")),
        refund_max_attempts=int(os.getenv("REFUND_MAX_ATTEMPTS", "10")),
        agent_price_usdt=agent_price_usdt,
        has_quote=_env_bool("AGENT_HAS_QUOTE", False),
        rate_limit_requests=int(os.getenv("RATE_LIMIT_REQUESTS", "120")),
        rate_limit_window=int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60")),
        trusted_proxy_ips=frozenset(
            ip.strip() for ip in os.getenv("TRUSTED_PROXY_IPS", "").split(",") if ip.strip()
        ),
        free_claim_limit=int(os.getenv("FREE_CLAIM_LIMIT", "1")),
        free_claim_window_seconds=int(os.getenv("FREE_CLAIM_WINDOW_SECONDS", "2592000")),
        file_store_dir=os.getenv("FILE_STORE_DIR", "file_store"),
        file_store_ttl=int(os.getenv("FILE_STORE_TTL", "900")),
        images_dir=os.getenv("IMAGES_DIR", "images"),
        agent_preview_url=os.getenv("AGENT_PREVIEW_URL") or None,
        agent_avatar_url=os.getenv("AGENT_AVATAR_URL") or None,
        agent_images=tuple(
            u.strip() for u in os.getenv("AGENT_IMAGES", "").split(",") if u.strip()
        ),
        owner_wallet=owner_ton,
        owner_wallet_sol=owner_sol,
        skus=skus,
        payment_rails=tuple(rails),
        tg_bot_token=tg_bot_token,
        tg_user_ids=tg_user_ids,
        usdt_master=(
            _chain_env("TON", "USDT_MASTER", "USDT_MASTER")
            or (USDT_MASTER_TESTNET if testnet else USDT_MASTER_MAINNET)
        ),
        jetton_wallet_code_hex=(
            (_chain_env("TON", "JETTON_WALLET_CODE", "JETTON_WALLET_CODE") or "").strip()
            or None
        ),
        sol_wallet_pk=sol_wallet_pk,
        sol_wallet=sol_wallet,
        sol_rpc_url=sol_rpc_url,
        sol_cluster=sol_cluster,
        sol_usdc_mint=sol_usdc_mint,
        agent_price_usdc=agent_price_usdc,
        agent_price_sol=agent_price_sol,
        sol_refund_fee_lamports=sol_refund_fee_lamports,
        sol_usdc_refund_fee=sol_usdc_refund_fee,
        sol_usdc_refund_ata_fee=sol_usdc_refund_ata_fee,
        sol_usdc_source_cooldown_seconds=sol_usdc_source_cooldown_seconds,
        sol_usdc_ip_max_failed_settles=sol_usdc_ip_max_failed_settles,
        sol_usdc_ip_failure_window_seconds=sol_usdc_ip_failure_window_seconds,
        sol_commitment=sol_commitment,
        sol_registry_address=sol_registry_address,
    )
