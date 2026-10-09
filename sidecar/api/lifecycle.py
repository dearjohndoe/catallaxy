from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any

from chains.ton.heartbeat import HeartbeatConfig, HeartbeatManager
from owner_bot import OwnerBot

from api.constants import DESCRIBE_TIMEOUT
from api.describe import fetch_describe
from api.domain.refund_worker import recover_stale_intents, refund_worker_loop
from api.infra.balancer_rebuild import balancer_rebuild_loop

if TYPE_CHECKING:
    from api.app import SidecarApp

logger = logging.getLogger("sidecar")


def build_heartbeats(app: "SidecarApp") -> list[HeartbeatManager]:
    """One heartbeat manager per registry the agent has a key for: TON with a
    TON key, Solana with a SOL key and a registry address."""
    hb_config = HeartbeatConfig(
        registry_address=app.settings.registry_address,
        endpoint=app.settings.agent_endpoint,
        price=app.settings.agent_price,
        capabilities=app.settings.capabilities,
        name=app.settings.agent_name,
        description=app.settings.agent_description,
        args_schema=app.args_schema,
        has_quote=app.settings.has_quote,
        price_usdt=app.settings.agent_price_usdt,
        sidecar_id=app.sidecar_id,
        result_schema=app.result_schema,
        preview_url=app.settings.agent_preview_url,
        avatar_url=app.settings.agent_avatar_url,
        images=app.settings.agent_images,
        owner_wallet=app.settings.owner_wallet,
        rails=app.settings.payment_rails,
        price_usdc=app.settings.agent_price_usdc,
        price_sol=app.settings.agent_price_sol,
    )
    heartbeats: list[HeartbeatManager] = []
    if app.sender is not None:
        heartbeats.append(HeartbeatManager(
            config=hb_config, state_store=app.state_store, transfer_sender=app.sender.send,
        ))
    if app.settings.sol_wallet_pk and app.settings.sol_registry_address:
        from chains.solana.registry import SolanaRegistry
        from chains.solana.rpc import SolanaRpc
        from chains.solana.wallet import parse_solana_keypair
        heartbeats.append(HeartbeatManager(
            config=hb_config,
            state_store=app.state_store,
            registry=SolanaRegistry(
                keypair=parse_solana_keypair(app.settings.sol_wallet_pk),
                rpc=SolanaRpc(app.settings.sol_rpc_url or ""),
                registry_address=app.settings.sol_registry_address,
            ),
            state_field="last_sol_heartbeat",
        ))
    elif app.settings.sol_wallet_pk:
        logger.warning("SOL_REGISTRY_ADDRESS not set: agent is not listed in the Solana registry")
    return heartbeats


async def startup(app: "SidecarApp") -> None:
    state = app.state_store.load()
    if state.sidecar_id is None:
        state.sidecar_id = str(uuid.uuid4())
        app.state_store.save(state)
    app.sidecar_id = state.sidecar_id

    app.args_schema, app.result_schema = await fetch_describe(
        app.settings.agent_command, DESCRIBE_TIMEOUT, app.sidecar_id,
    )
    if app.args_schema:
        logger.info("Agent args_schema loaded: %s", list(app.args_schema.keys()))
    else:
        logger.info("Agent returned no args_schema; validation disabled")
    if app.result_schema:
        logger.info("Agent result_schema loaded: %s", app.result_schema)

    app._file_store_dir.mkdir(parents=True, exist_ok=True)
    app._images_dir.mkdir(parents=True, exist_ok=True)

    await app.stock.init(app.settings.skus)

    app.heartbeats = build_heartbeats(app)

    def _silent_exception_handler(task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Background task failed unexpectedly")

    if app.verifier is not None:
        try:
            await app.verifier.start()
        except Exception:
            logger.exception("PaymentVerifier failed to start")

    sol_rail = app.rails.get("SOL")
    if sol_rail is not None:
        try:
            await sol_rail.start()
        except Exception:
            logger.exception("SOL rail monitor failed to start")

    if app.jetton_verifier:
        try:
            await app.jetton_verifier.start()
            app._agent_jetton_wallet = app.jetton_verifier.jetton_wallet_address
        except Exception:
            logger.exception("JettonPaymentVerifier failed to start")

    try:
        await app.tx_store.init()
    except Exception:
        logger.exception("ProcessedTxStore.init failed")

    try:
        await app.refund_queue.init()
    except Exception:
        logger.exception("RefundQueue.init failed")

    try:
        await app.free_claims.init()
    except Exception:
        logger.exception("FreeClaimStore.init failed")

    try:
        await app.claim_secrets.init()
    except Exception:
        logger.exception("ClaimSecretStore.init failed")

    if app.settings.tg_bot_token and app.settings.tg_user_ids:
        app.owner_bot = OwnerBot(
            token=app.settings.tg_bot_token,
            user_ids=app.settings.tg_user_ids,
            agent_name=app.settings.agent_name,
            agent_description=app.settings.agent_description,
            testnet=app.settings.testnet,
            sidecar_id=app.sidecar_id,
        )
        try:
            await app.owner_bot.setup()
        except Exception:
            logger.exception("OwnerBot.setup failed")

    # RAM jobs did not survive this process. Refund leftover accepted
    # intents before we start serving /invoke.
    try:
        await recover_stale_intents(app, older_than_seconds=0)
    except Exception:
        logger.exception("stale payment-intent recovery failed")

    # Heartbeat loops send right away when due; they don't block startup (a
    # TON send waits for the seqno to move, which can take a minute+).
    task_coros = [
        *(hb.loop(app.stop_event) for hb in app.heartbeats),
        app.cleanup_loop(),
        refund_worker_loop(app),
        balancer_rebuild_loop(app),
    ]
    if app.owner_bot is not None:
        task_coros.append(app.owner_bot.poll_loop(app.stop_event))
    for task_coro in task_coros:
        task = asyncio.create_task(task_coro)
        task.add_done_callback(_silent_exception_handler)
        app.background_tasks.append(task)


async def shutdown(app: "SidecarApp") -> None:
    app.stop_event.set()
    for task in app.background_tasks:
        task.cancel()
    await asyncio.gather(*app.background_tasks, return_exceptions=True)
    if app.sender is not None:
        await app.sender.close()
    for hb in app.heartbeats:
        close = getattr(hb.registry, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:
                logger.exception("%s registry close failed", hb.chain)
    for rail_id in ("USDC", "SOL"):
        rail = app.rails.get(rail_id)
        if rail is not None and hasattr(rail, "close"):
            try:
                await rail.close()
            except Exception:
                logger.exception("%s rail close failed", rail_id)
    if app.verifier is not None:
        await app.verifier.close()
    if app.jetton_verifier:
        await app.jetton_verifier.close()
    if app.tonapi_client is not None:
        await app.tonapi_client.close()
    await app.tx_store.close()
    await app.stock.close()
    await app.refund_queue.close()
    await app.free_claims.close()
    await app.claim_secrets.close()
    if app.owner_bot is not None:
        await app.owner_bot.close()
