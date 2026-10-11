"""Squads vault pays the sidecar (transfer + memo as CPIs); agent says oos → refund to vault."""
import asyncio, json, os, pathlib, subprocess, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # sidecar/
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aiohttp
from solders.keypair import Keypair
from solders.system_program import TransferParams, transfer
from chains.solana.rpc import SolanaRpc
from chains.solana.sol_monitor import parse_sol_payment
from e2e_invoke import send_ixs, cu, load, check, wait_balance, sol_preflight, claim, results

D = os.path.dirname(os.path.abspath(__file__))

async def main(seller_path, member_path, url):
    seller = load(seller_path)
    member = Keypair()
    json.dump(list(bytes(member)), open(member_path, "w"))
    rpc = SolanaRpc("https://api.devnet.solana.com")
    await send_ixs(rpc, seller, cu() + [transfer(TransferParams(
        from_pubkey=seller.pubkey(), to_pubkey=member.pubkey(), lamports=100_000_000))])
    async with aiohttp.ClientSession() as http:
        opt = await sol_preflight(http, url, "oos")
        out = subprocess.run(
            ["node", f"{D}/squads_pay.cjs", member_path, str(seller.pubkey()), opt["amount"], opt["memo"]],
            capture_output=True, text=True, env={**os.environ, "NODE_PATH": f"{D}/node_modules"},
            timeout=300)
        if out.returncode != 0:
            print(out.stderr[-3000:]); raise SystemExit(1)
        info = json.loads(out.stdout.strip().splitlines()[-1])
        print("squads:", info)
        raw = await rpc.get_transaction(info["executeSig"])
        top = [ix.get("program") or ix.get("programId") for ix in raw["transaction"]["message"]["instructions"]]
        inner = [(ix.get("program"), (ix.get("parsed") or {}) if isinstance(ix.get("parsed"), str) else (ix.get("parsed") or {}).get("type"))
                 for g in raw["meta"].get("innerInstructions") or [] for ix in g["instructions"]]
        print("   execute tx top-level:", top, "inner:", inner)
        p = parse_sol_payment(raw, signature=info["executeSig"], pay_to=str(seller.pubkey()))
        check("S parse: amount via delta, memo + sender from CPI",
              p.amount == int(opt["amount"]) and p.sender == info["vault"] and p.nonce and opt["memo"].endswith(p.nonce), p)
        vault_before = await rpc.get_balance(info["vault"])
        st, body = await claim(http, url, {"capability": "echo", "rail": "SOL", "proof": info["executeSig"],
                                           "nonce": opt["nonce"], "body": {"text": "oos"}})
        check("S Squads payment accepted by sidecar", st == 200 and "refund" in json.dumps(body), (st, body))
        got = await wait_balance(rpc, lambda: rpc.get_balance(info["vault"]), lambda v: v > vault_before)
        check("S refund went to the Squads vault", got - vault_before == int(opt["amount"]) - 5_000, got - vault_before)
    bal = await rpc.get_balance(str(member.pubkey()))
    await send_ixs(rpc, member, cu() + [transfer(TransferParams(
        from_pubkey=member.pubkey(), to_pubkey=seller.pubkey(), lamports=bal - 5_001))])
    print("cleanup: returned", bal - 5_001, "lamports from member; vault keeps the refund")
    await rpc.close()
    print("ALL PASS" if all(ok for _, ok, _ in results) else "FAILED")

asyncio.run(main(*sys.argv[1:4]))
