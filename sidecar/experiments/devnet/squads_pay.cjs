// Pay from a fresh Squads v4 multisig vault on devnet: SOL transfer + memo,
// both as CPIs inside vault_transaction_execute.
// usage: node squads_pay.cjs <member_keypair.json> <seller> <lamports> <memo>
const fs = require("fs");
const multisig = require("@sqds/multisig");
const {
  Connection, Keypair, PublicKey, SystemProgram, TransactionMessage,
  TransactionInstruction, sendAndConfirmTransaction, Transaction,
} = require("@solana/web3.js");

const MEMO = new PublicKey("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr");

async function main() {
  const [keyPath, sellerStr, lamportsStr, memo] = process.argv.slice(2);
  const conn = new Connection("https://api.devnet.solana.com", "confirmed");
  const member = Keypair.fromSecretKey(Uint8Array.from(JSON.parse(fs.readFileSync(keyPath))));
  const seller = new PublicKey(sellerStr);
  const lamports = Number(lamportsStr);
  const opts = { skipPreflight: false };
  const confirm = (sig) => conn.confirmTransaction(sig, "confirmed");

  const createKey = Keypair.generate();
  const [multisigPda] = multisig.getMultisigPda({ createKey: createKey.publicKey });
  const [programConfigPda] = multisig.getProgramConfigPda({});
  const programConfig = await multisig.accounts.ProgramConfig.fromAccountAddress(conn, programConfigPda);
  await confirm(await multisig.rpc.multisigCreateV2({
    connection: conn, treasury: programConfig.treasury, createKey, creator: member,
    multisigPda, configAuthority: null, threshold: 1,
    members: [{ key: member.publicKey, permissions: multisig.types.Permissions.all() }],
    timeLock: 0, rentCollector: null, sendOptions: opts,
  }));
  const [vaultPda] = multisig.getVaultPda({ multisigPda, index: 0 });

  // Fund the vault with exactly the payment so it ends at 0 (no rent dust).
  await sendAndConfirmTransaction(conn, new Transaction().add(
    SystemProgram.transfer({ fromPubkey: member.publicKey, toPubkey: vaultPda, lamports })), [member]);

  const { blockhash } = await conn.getLatestBlockhash();
  const transactionMessage = new TransactionMessage({
    payerKey: vaultPda, recentBlockhash: blockhash,
    instructions: [
      SystemProgram.transfer({ fromPubkey: vaultPda, toPubkey: seller, lamports }),
      new TransactionInstruction({ programId: MEMO, keys: [], data: Buffer.from(memo) }),
    ],
  });
  const transactionIndex = 1n;
  await confirm(await multisig.rpc.vaultTransactionCreate({
    connection: conn, feePayer: member, multisigPda, transactionIndex,
    creator: member.publicKey, vaultIndex: 0, ephemeralSigners: 0, transactionMessage, sendOptions: opts,
  }));
  await confirm(await multisig.rpc.proposalCreate({
    connection: conn, feePayer: member, creator: member, multisigPda, transactionIndex, sendOptions: opts,
  }));
  await confirm(await multisig.rpc.proposalApprove({
    connection: conn, feePayer: member, member, multisigPda, transactionIndex, sendOptions: opts,
  }));
  const executeSig = await multisig.rpc.vaultTransactionExecute({
    connection: conn, feePayer: member, multisigPda, transactionIndex,
    member: member.publicKey, sendOptions: opts,
  });
  await confirm(executeSig);
  console.log(JSON.stringify({ multisig: multisigPda.toBase58(), vault: vaultPda.toBase58(), executeSig }));
}

main().catch((e) => { console.error(e); process.exit(1); });
