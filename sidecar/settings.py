import hashlib
import os
import re
from dataclasses import dataclass


DEFAULT_SKU_ID = "default"

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


@dataclass
class Settings:
    agent_command: str
    capability: str
    agent_name: str
    agent_description: str
    agent_price: int
    agent_endpoint: str
    agent_wallet_pk: str
    agent_wallet_seed: str | None
    agent_wallet: str
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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


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


def _parse_sku_prices(price_spec: list[str], sku_id: str) -> tuple[int | None, int | None]:
    """Parse price tokens like ['ton=1000000000', 'usd=1500000']. At least one required."""
    price_ton: int | None = None
    price_usd: int | None = None
    for token in price_spec:
        token = token.strip()
        if not token:
            continue
        if "=" not in token:
            raise RuntimeError(f"SKU '{sku_id}': invalid price token '{token}' (expected ton=<n> or usd=<n>)")
        key, _, val = token.partition("=")
        key = key.strip().lower()
        val = val.strip()
        if not val:
            raise RuntimeError(f"SKU '{sku_id}': empty value for '{key}'")
        try:
            ival = int(val)
        except ValueError:
            raise RuntimeError(f"SKU '{sku_id}': '{key}' must be integer, got '{val}'")
        if ival < 0:
            raise RuntimeError(f"SKU '{sku_id}': '{key}' must be >= 0 (use 0 for dynamic pricing)")
        if key == "ton":
            price_ton = ival
        elif key == "usd":
            price_usd = ival
        else:
            raise RuntimeError(f"SKU '{sku_id}': unknown price key '{key}' (use ton or usd)")

    if price_ton is None and price_usd is None:
        raise RuntimeError(f"SKU '{sku_id}': at least one of ton/usd price required")

    return price_ton, price_usd


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

        price_ton, price_usd = _parse_sku_prices(parts[2:], sku_id)
        title = titles.get(sku_id) or sku_id
        skus.append(AgentSku(
            sku_id=sku_id, title=title,
            price_ton=price_ton, price_usd=price_usd,
            initial_stock=initial_stock,
        ))

    if not skus:
        raise RuntimeError("AGENT_SKUS is set but no valid SKU entries parsed")

    # All SKUs must share the same rail set
    rail_sets = {tuple(sorted([r for r, v in (("TON", s.price_ton), ("USD", s.price_usd)) if v is not None])) for s in skus}
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
        "AGENT_WALLET_PK",
    ]
    missing = [key for key in required_keys if not os.getenv(key)]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

    if not os.getenv("AGENT_PRICE") and not os.getenv("AGENT_PRICE_USD") and not os.getenv("AGENT_SKUS"):
        raise RuntimeError("Either AGENT_SKUS or AGENT_PRICE/AGENT_PRICE_USD must be set")

    agent_wallet_pk = os.environ["AGENT_WALLET_PK"]
    testnet = _env_bool("TESTNET", False)

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
    if skus[0].price_ton is None:
        agent_price = 0
    else:
        non_zero = [s.price_ton for s in skus if s.price_ton]
        agent_price = min(non_zero) if non_zero else 0
    if skus[0].price_usd is None:
        agent_price_usdt: int | None = None
    else:
        non_zero_usd = [s.price_usd for s in skus if s.price_usd]
        agent_price_usdt = min(non_zero_usd) if non_zero_usd else 0

    rails: list[str] = []
    if skus[0].price_ton is not None:
        rails.append("TON")
    if skus[0].price_usd is not None:
        rails.append("USDT")

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
    return Settings(
        agent_command=os.environ["AGENT_COMMAND"],
        capability=os.environ["AGENT_CAPABILITY"],
        agent_name=agent_name,
        agent_description=os.environ["AGENT_DESCRIPTION"],
        agent_price=agent_price,
        agent_endpoint=os.environ["AGENT_ENDPOINT"],
        agent_wallet=_derive_wallet_address(agent_wallet_pk, testnet),
        agent_wallet_pk=agent_wallet_pk,
        agent_wallet_seed=os.getenv("AGENT_WALLET_SEED"),
        registry_address=REGISTRY_ADDRESS,
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
        refund_fee_nanoton=int(os.getenv("REFUND_FEE_NANOTON", "500000")),
        refund_worker_interval=int(os.getenv("REFUND_WORKER_INTERVAL_SECONDS", "60")),
        refund_max_attempts=int(os.getenv("REFUND_MAX_ATTEMPTS", "10")),
        agent_price_usdt=agent_price_usdt,
        has_quote=_env_bool("AGENT_HAS_QUOTE", False),
        rate_limit_requests=int(os.getenv("RATE_LIMIT_REQUESTS", "60")),
        rate_limit_window=int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60")),
        trusted_proxy_ips=frozenset(
            ip.strip() for ip in os.getenv("TRUSTED_PROXY_IPS", "").split(",") if ip.strip()
        ),
        file_store_dir=os.getenv("FILE_STORE_DIR", "file_store"),
        file_store_ttl=int(os.getenv("FILE_STORE_TTL", "900")),
        images_dir=os.getenv("IMAGES_DIR", "images"),
        agent_preview_url=os.getenv("AGENT_PREVIEW_URL") or None,
        agent_avatar_url=os.getenv("AGENT_AVATAR_URL") or None,
        agent_images=tuple(
            u.strip() for u in os.getenv("AGENT_IMAGES", "").split(",") if u.strip()
        ),
        owner_wallet=os.getenv("OWNER_WALLET") or None,
        skus=skus,
        payment_rails=tuple(rails),
        tg_bot_token=tg_bot_token,
        tg_user_ids=tg_user_ids,
    )
