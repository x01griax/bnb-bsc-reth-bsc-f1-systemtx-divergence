//! F-1 controlled reproduction (Stage 4): system-transaction target-set
//! divergence — the reth-bsc half.
//!
//! Hypothesis under test (F-1 / H1d): bsc's parlia `systemContracts` map (14
//! entries) does NOT contain TokenManager (0x1008), Staking (0x2001) or
//! StakeCredit (0x2003), while reth-bsc's `SYSTEM_CONTRACTS_SET` (17 entries)
//! does. Therefore a transaction that is (a) signed by the block coinbase,
//! (b) has zero effective gas price and (c) targets 0x1008 is classified by
//! reth-bsc as a system transaction (`is_system_transaction`), skips the
//! regular execution path, sits unconsumed in the executor's system-tx queue,
//! and the leftover check in `post_check_new_block` rejects the block with
//! `UnexpectedSystemTx` — while bsc executes it as a NORMAL transaction and
//! accepts the block (verified by the bsc half of this differential:
//! `consensus/parlia/f1_systemtx_repro_test.go` in bnb-chain/bsc @ 947ba1c).
//!
//! The trigger transaction is byte-identical on both sides (same fixed key,
//! same nonce, same chain id 2, deterministic ECDSA), so both halves sign the
//! same tx hash (0x46b3...d407).
//!
//! Harness: drives the real `BscBlockExecutor` in Import mode — the same
//! `BscEvmConfig::bsc(..).executor_for_block(..)` pattern this crate's own
//! executor tests use — through `apply_pre_execution_changes`
//! (`check_new_block`: seal verification via `recover_proposer`, block-time
//! check against a mocked single-validator snapshot, payment-lane gate),
//! `execute_transaction_without_commit` (the classification site) and
//! `finish()` -> `post_check_new_block` (the leftover check that raises
//! `UnexpectedSystemTx`). Only the global snapshot provider is mocked: the
//! node-level parlia snapshot store is initialized from the real database,
//! which an in-memory unit test does not have.

use crate::{
    chainspec::{parser::parse_genesis_json, BscChainSpec},
    consensus::parlia::{hash_with_chain_id, Snapshot, SnapshotProvider, DIFF_INTURN},
    node::{
        evm::{config::BscEvmConfig, util::insert_header_to_cache_with_hash},
        primitives::{BscBlock, BscBlockBody},
    },
    shared::set_snapshot_provider,
};
use alloy_consensus::{Header, SignableTransaction, TxLegacy};
use alloy_evm::block::BlockExecutor;
use alloy_primitives::{address, hex, Address, Bytes, Signature, TxKind, B256, U256};
use reth_ethereum_primitives::{BlockBody, Transaction, TransactionSigned};
use reth_evm::ConfigureEvm;
use reth_primitives_traits::{Recovered, SealedBlock, SignerRecoverable};
use revm::{database::InMemoryDB, state::AccountInfo};
use secp256k1::{Message, Secp256k1, SecretKey};
use std::sync::Arc;

/// Same fixed validator key as the bsc half of the differential.
const F1_VALIDATOR_KEY_HEX: [u8; 32] =
    hex!("8f2a4b1c3d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8");
const F1_SENDER2_KEY_HEX: [u8; 32] =
    hex!("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef");

/// The trigger target: in reth-bsc's SYSTEM_CONTRACTS_SET but NOT in bsc's
/// parlia systemContracts map (the F-1 delta).
const F1_TOKEN_MANAGER: Address = address!("0000000000000000000000000000000000001008");
/// In BOTH clients' system-contract sets (control).
const F1_VALIDATOR_CONTRACT: Address = address!("0000000000000000000000000000000000001000");

/// The trigger tx hash computed by the bsc half of the differential
/// (bsc @ 947ba1c, TestF1SystemTxTargetSetDivergence).
const F1_GO_TRIGGER_TX_HASH: [u8; 32] =
    hex!("46b35a1d3b68d5053a0e8b455918866547f4dd1dc660283e19730ef0b817d407");

/// Block shape mirroring the bsc harness: block 2 at timestamp 20 whose
/// parent (block 1, timestamp 10) carried the seven initContract system txs
/// that bumped the coinbase nonce to 7.
const F1_PARENT_NUMBER: u64 = 1;
const F1_PARENT_TIMESTAMP: u64 = 10;
const F1_BLOCK_NUMBER: u64 = 2;
const F1_BLOCK_TIMESTAMP: u64 = 20;
const F1_GAS_LIMIT: u64 = 30_000_000;
const F1_COINBASE_NONCE: u64 = 7;

const F1_ONE_ETH: u128 = 1_000_000_000_000_000_000;

/// Post-Pasteur / pre-Jenner spec (the regime BSC mainnet is in today): every
/// fork through Pasteur at genesis, Jenner NOT scheduled.
fn f1_spec() -> Arc<BscChainSpec> {
    parse_genesis_json(
        r#"{
            "config": {
                "chainId": 2,
                "ramanujanBlock": 0, "nielsBlock": 0, "berlinBlock": 0, "londonBlock": 0,
                "brunoBlock": 0, "eulerBlock": 0, "nanoBlock": 0, "moranBlock": 0,
                "gibbsBlock": 0, "planckBlock": 0, "lubanBlock": 0, "platoBlock": 0,
                "hertzBlock": 0, "hertzfixBlock": 0,
                "shanghaiTime": 0, "keplerTime": 0, "feynmanTime": 0, "cancunTime": 0,
                "haberTime": 0, "bohrTime": 0, "pragueTime": 0, "pascalTime": 0,
                "lorentzTime": 0, "maxwellTime": 0, "fermiTime": 0, "osakaTime": 0,
                "mendelTime": 0, "pasteurTime": 0
            },
            "difficulty": "0x1",
            "gasLimit": "0x1DCD6500",
            "alloc": {}
        }"#,
    )
    .expect("genesis should parse")
}

fn f1_address_of(key: &SecretKey, secp: &Secp256k1<secp256k1::All>) -> Address {
    let public = key.public_key(secp);
    Address::from_slice(&alloy_primitives::keccak256(&public.serialize_uncompressed()[1..])[12..])
}

/// Deterministically signs `hash` with `key` (RFC-6979, the same construction
/// go-bsc's crypto.Sign uses), returning the 65-byte
/// compact-signature || recovery-id form.
fn f1_sign(secp: &Secp256k1<secp256k1::All>, key: &SecretKey, hash: &B256) -> [u8; 65] {
    // Same construction as the node's own MinerSigner::seal_header.
    let msg = Message::from_digest(hash.0);
    let sig = secp.sign_ecdsa_recoverable(&msg, key);
    let (recovery_id, signature_bytes) = sig.serialize_compact();
    let mut out = [0u8; 65];
    out[..64].copy_from_slice(&signature_bytes);
    out[64] = i32::from(recovery_id) as u8;
    out
}

fn f1_signature(out: &[u8; 65]) -> Signature {
    Signature::new(
        U256::from_be_slice(&out[..32]),
        U256::from_be_slice(&out[32..64]),
        out[64] % 2 == 1,
    )
}

/// Builds the trigger-shape tx: legacy, chain id 2, zero-value, 21000 gas.
fn f1_tx(
    secp: &Secp256k1<secp256k1::All>,
    key: &SecretKey,
    nonce: u64,
    to: Address,
    gas_price: u128,
) -> TransactionSigned {
    let legacy = TxLegacy {
        chain_id: Some(2),
        nonce,
        gas_price,
        gas_limit: 21_000,
        to: TxKind::Call(to),
        value: U256::ZERO,
        input: Bytes::new(),
    };
    let sighash = legacy.signature_hash();
    TransactionSigned::new_unhashed(Transaction::Legacy(legacy), f1_signature(&f1_sign(secp, key, &sighash)))
}

/// A single-validator parlia snapshot for the mocked global provider.
struct F1SnapshotProvider {
    snapshot: Snapshot,
}

impl SnapshotProvider for F1SnapshotProvider {
    fn snapshot_by_hash(&self, _block_hash: &B256) -> Option<Snapshot> {
        Some(self.snapshot.clone())
    }
    fn insert(&self, _snapshot: Snapshot) {}
}

/// Identities + the mocked global snapshot provider (single validator,
/// in-turn always, epoch length 200, pre-Lorentz 3s block interval).
fn f1_setup() -> (Arc<BscChainSpec>, Address, Address, Secp256k1<secp256k1::All>, SecretKey, SecretKey) {
    let secp = Secp256k1::new();
    let validator_key = SecretKey::from_slice(&F1_VALIDATOR_KEY_HEX).expect("validator key");
    let sender2_key = SecretKey::from_slice(&F1_SENDER2_KEY_HEX).expect("sender2 key");
    let validator = f1_address_of(&validator_key, &secp);
    let sender2 = f1_address_of(&sender2_key, &secp);

    let mut snapshot = Snapshot::new(vec![validator], F1_PARENT_NUMBER, B256::ZERO, 200, None);
    snapshot.block_interval = 3000; // ms, pre-Lorentz default
    // Global OnceLock: the first test to run installs it; the rest reuse it
    // (every case uses the same snapshot, so this is fine).
    let _ = set_snapshot_provider(Arc::new(F1SnapshotProvider { snapshot }));

    (f1_spec(), validator, sender2, secp, validator_key, sender2_key)
}

/// Builds the sealed trigger block: header sealed by the validator (32B vanity
/// + 65B ECDSA over the chain-id seal hash), in-turn difficulty 2, BSC zero
/// base fee, body carrying the trigger tx. Also caches the parent header so
/// `check_new_block` can resolve it.
fn f1_block(validator: Address, secp: &Secp256k1<secp256k1::All>, validator_key: &SecretKey, tx: &TransactionSigned) -> SealedBlock<BscBlock> {
    let parent = Header {
        number: F1_PARENT_NUMBER,
        timestamp: F1_PARENT_TIMESTAMP,
        gas_limit: F1_GAS_LIMIT,
        ..Default::default()
    };
    insert_header_to_cache_with_hash(parent.clone(), None);

    let mut header = Header {
        number: F1_BLOCK_NUMBER,
        timestamp: F1_BLOCK_TIMESTAMP,
        parent_hash: parent.hash_slow(),
        beneficiary: validator,
        gas_limit: F1_GAS_LIMIT,
        difficulty: DIFF_INTURN,
        base_fee_per_gas: Some(0), // BSC never enforces a base fee
        excess_blob_gas: Some(0),
        blob_gas_used: Some(0),
        ..Default::default()
    };
    // The seal hash covers everything except the last 65 bytes of extra_data,
    // so the 32-byte vanity must be in place BEFORE signing (MinerSigner::
    // seal_header computes it over the fully-formed header the same way).
    header.extra_data = Bytes::from(vec![0u8; 97]); // 32B vanity + 65B placeholder seal
    let seal = f1_sign(secp, validator_key, &hash_with_chain_id(&header, 2));
    let mut extra = header.extra_data.to_vec();
    extra[32..97].copy_from_slice(&seal);
    header.extra_data = Bytes::from(extra);

    SealedBlock::seal_slow(BscBlock {
        header,
        body: BscBlockBody {
            inner: BlockBody { transactions: vec![tx.clone()], ..Default::default() },
            sidecars: None,
        },
    })
}

/// Seeds the in-memory state: coinbase (nonce 7 — block 1 of the mirrored
/// chain carries seven initContract system txs) and the second sender.
fn f1_state_db(validator: Address, sender2: Address) -> InMemoryDB {
    let mut db = InMemoryDB::default();
    db.insert_account_info(
        validator,
        AccountInfo { balance: U256::from(F1_ONE_ETH), nonce: F1_COINBASE_NONCE, ..Default::default() },
    );
    db.insert_account_info(
        sender2,
        AccountInfo { balance: U256::from(F1_ONE_ETH), nonce: 0, ..Default::default() },
    );
    db
}

/// Runs one trigger block through the executor pipeline and returns the
/// `finish()` outcome: `Ok` = block accepted, `Err` = rejected. The block is
/// always proposed by the validator; the tx may be signed by anyone.
fn f1_execute_block(
    spec: &Arc<BscChainSpec>,
    validator: Address,
    sender2: Address,
    secp: &Secp256k1<secp256k1::All>,
    validator_key: &SecretKey,
    tx: TransactionSigned,
) -> Result<(), alloy_evm::block::BlockExecutionError> {
    let recovered: Recovered<TransactionSigned> =
        tx.clone().try_into_recovered().expect("signature must recover");
    let block = f1_block(validator, secp, validator_key, &tx);
    let mut state_db = revm::database::State::builder().with_database(f1_state_db(validator, sender2)).build();
    let evm_config = BscEvmConfig::bsc(spec.clone());
    let mut executor = evm_config
        .executor_for_block(&mut state_db, &block)
        .expect("executor must construct");
    executor.apply_pre_execution_changes()?;
    executor.execute_transaction_without_commit(recovered)?;
    let (_evm, _result) = executor.finish()?;
    Ok(())
}

/// MAIN CASE — the F-1 trigger: a coinbase-signed, zero-gas-price tx to
/// TokenManager (0x1008). reth-bsc must classify it as a system transaction,
/// leave it unconsumed in the system-tx queue, and reject the block from
/// `finish()`/`post_check_new_block` with `UnexpectedSystemTx` — while bsc
/// accepts the same block (see the Go half).
#[test]
fn f1_coinbase_zero_gas_tx_to_token_manager_is_unexpected_system_tx() {
    let (spec, validator, sender2, secp, validator_key, _sender2_key) = f1_setup();

    let tx = f1_tx(&secp, &validator_key, F1_COINBASE_NONCE, F1_TOKEN_MANAGER, 0);
    // The signer must genuinely recover to the coinbase: the classification
    // predicate is (signer == coinbase && zero effective gas price && to in
    // SYSTEM_CONTRACTS_SET).
    let recovered: Recovered<TransactionSigned> =
        tx.clone().try_into_recovered().expect("signature must recover");
    assert_eq!(recovered.signer(), validator, "trigger signer must be the coinbase");

    let err = f1_execute_block(&spec, validator, sender2, &secp, &validator_key, tx)
        .expect_err("F-1 MAIN CASE: reth-bsc must reject the trigger block with UnexpectedSystemTx");
    let msg = err.to_string();
    assert!(
        msg.contains("unexpected system tx"),
        "wrong rejection reason, want 'unexpected system tx', got: {msg}"
    );
    println!("F-1 MAIN CASE: reth-bsc rejected the trigger block with: {msg}");
}

/// Cross-client tx identity: the trigger built here must hash to exactly what
/// the bsc half computed (same key, same fields, deterministic ECDSA),
/// proving both halves of the differential tested the same transaction.
#[test]
fn f1_trigger_tx_hash_matches_go_bsc_reproduction() {
    let (_spec, _validator, _sender2, secp, validator_key, _sender2_key) = f1_setup();
    let tx = f1_tx(&secp, &validator_key, F1_COINBASE_NONCE, F1_TOKEN_MANAGER, 0);
    assert_eq!(
        tx.tx_hash().as_slice(),
        &F1_GO_TRIGGER_TX_HASH[..],
        "the trigger tx must be byte-identical to the bsc half of the differential"
    );
}

/// CONTROL — same tx shape but with a non-zero gas price (1 gwei): not a
/// system transaction on EITHER client, so reth-bsc must accept the block.
#[test]
fn f1_control_paid_gas_tx_to_token_manager_is_accepted() {
    let (spec, validator, sender2, secp, validator_key, _sender2_key) = f1_setup();
    let tx = f1_tx(&secp, &validator_key, F1_COINBASE_NONCE, F1_TOKEN_MANAGER, 1_000_000_000);
    f1_execute_block(&spec, validator, sender2, &secp, &validator_key, tx)
        .expect("CONTROL: reth-bsc must accept the paid-gas trigger block");
    println!("F-1 CONTROL: paid-gas (1 gwei) trigger accepted as a normal tx");
}

/// CONTROL — zero-gas tx to 0x1008 from a NON-coinbase sender: the predicate
/// requires signer == coinbase, so this is a normal tx on BOTH clients and
/// the block must be accepted.
#[test]
fn f1_control_non_coinbase_zero_gas_tx_to_token_manager_is_accepted() {
    let (spec, validator, sender2, secp, validator_key, sender2_key) = f1_setup();
    let tx = f1_tx(&secp, &sender2_key, 0, F1_TOKEN_MANAGER, 0);
    let recovered: Recovered<TransactionSigned> =
        tx.clone().try_into_recovered().expect("signature must recover");
    assert_ne!(recovered.signer(), validator, "sanity: control sender is not the coinbase");
    // The block is still proposed (and sealed) by the validator; only the tx
    // sender differs.
    f1_execute_block(&spec, validator, sender2, &secp, &validator_key, tx)
        .expect("CONTROL: reth-bsc must accept the non-coinbase trigger block");
    println!("F-1 CONTROL: non-coinbase zero-gas trigger accepted as a normal tx");
}

/// CONTROL — the same trigger shape pointed at the ValidatorContract
/// (0x1000), which is in BOTH clients' system-contract sets: both clients
/// agree this is a system transaction, and with no generated system call to
/// consume it, reth-bsc rejects the block (bsc rejects it too with
/// "the length of systemTxs do not match" — see the Go half).
#[test]
fn f1_control_coinbase_zero_gas_tx_to_validator_contract_is_rejected() {
    let (spec, validator, sender2, secp, validator_key, _sender2_key) = f1_setup();
    let tx = f1_tx(&secp, &validator_key, F1_COINBASE_NONCE, F1_VALIDATOR_CONTRACT, 0);
    let err = f1_execute_block(&spec, validator, sender2, &secp, &validator_key, tx)
        .expect_err("CONTROL: validator-contract system tx must be rejected as unexpected");
    assert!(
        err.to_string().contains("unexpected system tx"),
        "wrong rejection reason, got: {err}"
    );
    println!("F-1 CONTROL: validator-contract (0x1000) system tx rejected: {err}");
}
