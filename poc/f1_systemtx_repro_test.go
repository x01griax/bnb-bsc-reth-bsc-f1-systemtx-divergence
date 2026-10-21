// Copyright 2026 The go-ethereum Authors
// This file is part of the go-ethereum library.
//
// The go-ethereum library is free software: you can redistribute it and/or modify
// it under the terms of the GNU Lesser General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.

// F-1 controlled reproduction (Stage 4): system-transaction target-set divergence.
//
// Hypothesis under test (F-1 / H1d): reth-bsc's SYSTEM_CONTRACTS_SET treats
// TokenManager (0x1008), Staking (0x2001) and StakeCredit (0x2003) as
// system-transaction targets, while bsc's parlia systemContracts map (14
// entries) does not. Therefore a transaction that is (a) signed by the block
// coinbase, (b) has zero effective gas price and (c) targets one of those
// three addresses is classified as a NORMAL transaction by bsc (executed via
// the regular state-processor path with a normal receipt) while reth-bsc
// classifies it as a system transaction and rejects the block
// (UnexpectedSystemTx). This file is the bsc half of the differential: it
// proves bsc ACCEPTS such a block, exercising the real classification
// predicate (Parlia.IsSystemTransaction) plus the full block import pipeline.
//
// The reth-bsc half (same trigger fields, same fixed key) lives in the
// reth-bsc repo as src/node/evm/f1_system_tx_divergence_tests.rs.
//
// To keep the unit test focused on classification/execution (and not on seal
// cryptography, which is orthogonal to F-1), the engine wrapper below stubs
// out header (seal) verification only. Everything that matters for F-1 — the
// PoSA transaction classification hook, normal tx execution, Parlia.Finalize
// (including the system-tx leftover check "the length of systemTxs do not
// match" and the block-1 initContract system-tx generation) — runs against the
// REAL *Parlia engine.
package parlia

import (
        "fmt"
        "math/big"
        "strings"
        "testing"

        "github.com/ethereum/go-ethereum/accounts"
        "github.com/ethereum/go-ethereum/common"
        "github.com/ethereum/go-ethereum/consensus"
        "github.com/ethereum/go-ethereum/core"
        "github.com/ethereum/go-ethereum/core/rawdb"
        "github.com/ethereum/go-ethereum/core/state"
        "github.com/ethereum/go-ethereum/core/systemcontracts"
        "github.com/ethereum/go-ethereum/core/types"
        "github.com/ethereum/go-ethereum/core/tracing"
        "github.com/ethereum/go-ethereum/crypto"
        "github.com/ethereum/go-ethereum/params"
        "github.com/ethereum/go-ethereum/triedb"
)

// Deterministic test keys (fixed so tx/block hashes are reproducible and can
// be cross-referenced with the reth-bsc half of the differential, which uses
// the same key and produces the same signature via deterministic ECDSA).
const (
        f1ValidatorKeyHex = "8f2a4b1c3d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8"
        f1Sender2KeyHex   = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
)

// f1Sender2 is a second funded account used for the non-coinbase control.
var f1Sender2 = common.HexToAddress("0x9999999999999999999999999999999999999999")

// f1Config returns a Parlia chain config with every fork through Pasteur
// active at genesis and Jenner NOT scheduled: the regime BSC mainnet is in
// today (post-Pasteur, pre-Jenner). Forks are pinned so no time-based fork
// flips mid-test (block times are 10s and 20s).
func f1Config() *params.ChainConfig {
        cfg := *params.ParliaTestChainConfig
        cfg.HaberTime = new(uint64)
        cfg.HaberFixTime = new(uint64)
        cfg.BohrTime = new(uint64)
        cfg.PascalTime = new(uint64)
        cfg.PragueTime = new(uint64)
        cfg.LorentzTime = new(uint64)
        cfg.MaxwellTime = new(uint64)
        cfg.FermiTime = new(uint64)
        cfg.OsakaTime = new(uint64)
        cfg.MendelTime = new(uint64)
        cfg.PasteurTime = new(uint64)
        cfg.JennerTime = nil // pre-Jenner: BEP-703 payment lane inactive, matches mainnet today
        cfg.Parlia = &params.ParliaConfig{}
        // The blob schedule must cover every scheduled fork (Genesis.MustCommit
        // panics otherwise), exactly like jennerTestChainConfig in core.
        cfg.BlobScheduleConfig = &params.BlobScheduleConfig{
                Cancun: params.DefaultCancunBlobConfig,
                Prague: params.DefaultPragueBlobConfigBSC,
                Osaka:  params.DefaultOsakaBlobConfigBSC,
        }
        return &cfg
}

// f1NoSealEngine embeds the real *Parlia engine and stubs out ONLY header
// (seal) verification and difficulty computation:
//   - VerifyHeader / VerifyHeaders: accept everything (Parlia seal checks —
//     ECDSA signature over the header seal-hash, validator membership, turn
//     timing — are authentication concerns orthogonal to F-1; the stage
//     directive explicitly allows mocking headers).
//   - CalcDifficulty: constant in-turn (2) so Finalize skips the
//     slash-spoiled-validator path.
//   - FinalizeAndAssemble: delegates to the REAL Parlia implementation, but
//     through a chain reader that can also resolve the genesis block: core's
//     chainMaker does not index the genesis parent, while Parlia's
//     FinalizeAndAssemble needs the parent header for fork-transition checks.
//
// Everything else — IsSystemTransaction (the F-1 classification predicate),
// Prepare, Finalize (incl. system-tx generation, matching and the leftover
// check) — is the REAL Parlia implementation.
type f1NoSealEngine struct {
        *Parlia
        genesisHash   common.Hash
        genesisHeader *types.Header
}

// f1ChainReaderWithGenesis resolves the genesis header that core's
// chainMaker (block generation) does not index.
type f1ChainReaderWithGenesis struct {
        consensus.ChainHeaderReader
        genesisHash   common.Hash
        genesisHeader *types.Header
}

func (r f1ChainReaderWithGenesis) GetHeaderByHash(hash common.Hash) *types.Header {
        if hash == r.genesisHash {
                return r.genesisHeader
        }
        return r.ChainHeaderReader.GetHeaderByHash(hash)
}

func (e *f1NoSealEngine) FinalizeAndAssemble(chain consensus.ChainHeaderReader, header *types.Header, state *state.StateDB, body *types.Body, receipts []*types.Receipt, tracer *tracing.Hooks) (*types.Block, []*types.Receipt, error) {
        return e.Parlia.FinalizeAndAssemble(
                f1ChainReaderWithGenesis{ChainHeaderReader: chain, genesisHash: e.genesisHash, genesisHeader: e.genesisHeader},
                header, state, body, receipts, tracer)
}

func (e *f1NoSealEngine) VerifyHeader(chain consensus.ChainHeaderReader, header *types.Header) error {
        return nil
}

func (e *f1NoSealEngine) VerifyHeaders(chain consensus.ChainHeaderReader, headers []*types.Header) (chan<- struct{}, <-chan error) {
        abort := make(chan struct{})
        results := make(chan error, len(headers))
        for i := 0; i < len(headers); i++ {
                results <- nil
        }
        return abort, results
}

func (e *f1NoSealEngine) CalcDifficulty(chain consensus.ChainHeaderReader, time uint64, parent *types.Header) *big.Int {
        return diffInTurn
}

// f1GenesisExtra builds a post-Luban/post-Bohr epoch-block extra field with a
// single validator: |32B vanity|1B count|20B validator|48B BLS vote addr|1B
// turn length|65B seal|. Genesis is an epoch block (number 0), so the Parlia
// snapshot machinery bootstraps its validator set from this field.
func f1GenesisExtra(validator common.Address) []byte {
        // |32B vanity|1B count|20B consensus addr|48B BLS vote addr|1B turn length|65B seal|
        // (validatorBytesLength = common.AddressLength + types.BLSPublicKeyLength = 20+48.)
        extra := make([]byte, 0, 32+1+68+1+65)
        extra = append(extra, make([]byte, 32)...)  // vanity
        extra = append(extra, byte(1))              // validator count
        extra = append(extra, validator.Bytes()...) // consensus address
        extra = append(extra, make([]byte, 48)...)  // BLS vote address (zeros)
        extra = append(extra, byte(1))              // turn length
        extra = append(extra, make([]byte, 65)...)  // genesis seal placeholder
        return extra
}

// f1RunChain builds a two-block chain on a fresh database and imports it
// through the real core.BlockChain pipeline:
//
//      block 1: no user txs; Parlia.FinalizeAndAssemble auto-appends the seven
//               initContract system txs (validator-signed, zero gas) that block 1
//               of every real BSC chain carries.
//      block 2: exactly one tx produced by mkTx (the F-1 trigger under test).
//
// It returns the chain (for post-conditions), the generated blocks and the
// InsertChain error (nil = block accepted).
func f1RunChain(t *testing.T, mkTx func(signer types.Signer, gen *core.BlockGen) *types.Transaction) (*core.BlockChain, []*types.Block, error) {
        t.Helper()

        key, err := crypto.HexToECDSA(f1ValidatorKeyHex)
        if err != nil {
                t.Fatalf("bad test key: %v", err)
        }
        validator := crypto.PubkeyToAddress(key.PublicKey)
        cfg := f1Config()

        db := rawdb.NewMemoryDatabase()
        gspec := &core.Genesis{
                Config:    cfg,
                Coinbase:  validator,
                ExtraData: f1GenesisExtra(validator),
                GasLimit:  30_000_000,
                Alloc: types.GenesisAlloc{
                        validator: {Balance: new(big.Int).SetUint64(params.Ether)},
                        f1Sender2: {Balance: new(big.Int).SetUint64(params.Ether)},
                },
        }
        genesis := gspec.MustCommit(db, triedb.NewDatabase(db, nil))
        t.Logf("validator (coinbase): %s", validator.Hex())

        signer := types.LatestSigner(cfg)
        engine := &f1NoSealEngine{Parlia: New(cfg, db, nil, genesis.Hash()), genesisHash: genesis.Hash(), genesisHeader: genesis.Header()}
        engine.Authorize(validator, nil, func(account accounts.Account, tx *types.Transaction, chainID *big.Int) (*types.Transaction, error) {
                if account.Address != validator {
                        return nil, fmt.Errorf("unexpected signing account %s", account.Address)
                }
                return types.SignTx(tx, signer, key)
        })

        blocks, _ := core.GenerateChain(cfg, genesis, engine, db, 2, func(i int, gen *core.BlockGen) {
                gen.SetCoinbase(validator)
                if i == 1 {
                        if tx := mkTx(signer, gen); tx != nil {
                                gen.AddTx(tx)
                        }
                }
        })

        // Seal the generated blocks like a real Parlia proposer would (32B vanity +
        // 65B ECDSA signature over types.SealHash). GenerateChain produces unsealed
        // headers; the Parlia snapshot machinery (ecrecover during snapshot.apply,
        // reached from core.NewHeaderChain and the finality bookkeeping in
        // InsertChain) requires every non-genesis header to carry a valid seal,
        // even though header verification itself is stubbed out on the engine.
        for i, b := range blocks {
                h := types.CopyHeader(b.Header())
                // Relink to the previous block's SEALED hash (sealing changes the
                // hash, so the unsealed ParentHash from GenerateChain is stale).
                if i > 0 {
                        h.ParentHash = blocks[i-1].Hash()
                }
                // SealHash strips the 65-byte seal suffix from Extra, so Extra must
                // already be at least 97 bytes (32B vanity + 65B placeholder seal).
                h.Extra = make([]byte, 97)
                sig, err := crypto.Sign(types.SealHash(h, cfg.ChainID).Bytes(), key)
                if err != nil {
                        t.Fatalf("sealing block %d: %v", b.NumberU64(), err)
                }
                copy(h.Extra[32:], sig)
                blocks[i] = b.WithSeal(h)
        }

        chain, err := core.NewBlockChain(db, gspec, engine, nil)
        if err != nil {
                t.Fatalf("NewBlockChain: %v", err)
        }
        t.Cleanup(chain.Stop)

        for i, blk := range blocks {
                if _, err := chain.InsertChain([]*types.Block{blk}); err != nil {
                        return chain, blocks, fmt.Errorf("inserting block %d (chain index %d): %w", blk.NumberU64(), i, err)
                }
        }
        return chain, blocks, nil
}

// f1SignedTx builds and signs the trigger tx shape: legacy tx to `to`, with
// the given gas price, signed by the given hex key at the given nonce.
func f1SignedTx(signer types.Signer, keyHex string, nonce uint64, to common.Address, gasPrice *big.Int) *types.Transaction {
        key, err := crypto.HexToECDSA(keyHex)
        if err != nil {
                panic(err)
        }
        tx, err := types.SignNewTx(key, signer, &types.LegacyTx{
                Nonce:    nonce,
                To:       &to,
                Gas:      params.TxGas,
                GasPrice: gasPrice,
        })
        if err != nil {
                panic(err)
        }
        return tx
}

// TestF1SystemTxTargetSetDivergence is the bsc half of the F-1 controlled
// reproduction: a coinbase-signed zero-gas tx to TokenManager (0x1008) is a
// NORMAL transaction for bsc, so the block imports cleanly.
func TestF1SystemTxTargetSetDivergence(t *testing.T) {
        tokenManager := common.HexToAddress(systemcontracts.TokenManagerContract)
        validatorContract := common.HexToAddress(systemcontracts.ValidatorContract)

        // The trigger must be exactly the divergent triple:
        // sender == coinbase && effective gas price == 0 && to == 0x1008.
        t.Run("Main_CoinbaseZeroGasTxToTokenManager_Accepted", func(t *testing.T) {
                var trigger *types.Transaction
                chain, blocks, err := f1RunChain(t, func(signer types.Signer, gen *core.BlockGen) *types.Transaction {
                        key, _ := crypto.HexToECDSA(f1ValidatorKeyHex)
                        trigger = f1SignedTx(signer, f1ValidatorKeyHex, gen.TxNonce(crypto.PubkeyToAddress(key.PublicKey)), tokenManager, new(big.Int))
                        return trigger
                })
                if err != nil {
                        t.Fatalf("F-1 main case FAILED: bsc rejected the trigger block, but the hypothesis predicts acceptance: %v", err)
                }
                // Post-conditions proving the trigger tx was executed as a NORMAL tx:
                block2 := blocks[1]
                if got := len(block2.Transactions()); got != 1 {
                        t.Fatalf("block 2 should carry exactly the trigger tx, has %d", got)
                }
                tx := block2.Transactions()[0]
                if tx.Hash() != trigger.Hash() {
                        t.Fatalf("block 2 tx %v != trigger %v", tx.Hash(), trigger.Hash())
                }
                if tx.To() == nil || *tx.To() != tokenManager {
                        t.Fatalf("trigger tx to = %v, want %s", tx.To(), tokenManager.Hex())
                }
                if tx.GasPrice().Sign() != 0 {
                        t.Fatalf("trigger tx gas price = %v, want 0", tx.GasPrice())
                }
                from, err := types.Sender(types.LatestSigner(f1Config()), tx)
                if err != nil {
                        t.Fatalf("sender recovery: %v", err)
                }
                if from != block2.Coinbase() {
                        t.Fatalf("trigger sender %s != coinbase %s (precondition of the trigger)", from.Hex(), block2.Coinbase().Hex())
                }
                receipts := chain.GetReceiptsByHash(block2.Hash())
                if len(receipts) != 1 {
                        t.Fatalf("block 2 should have 1 receipt (normal execution), has %d", len(receipts))
                }
                if receipts[0].Status != types.ReceiptStatusSuccessful {
                        t.Fatalf("trigger receipt status = %d, want 1 (successful normal execution)", receipts[0].Status)
                }
                if receipts[0].GasUsed != params.TxGas {
                        t.Fatalf("trigger receipt gasUsed = %d, want %d (plain value-transfer semantics => normal classification)", receipts[0].GasUsed, params.TxGas)
                }
                if chain.GetBlockByNumber(2) == nil || chain.GetBlockByNumber(2).Hash() != block2.Hash() {
                        t.Fatalf("block 2 not canonical after import")
                }
                t.Logf("F-1 MAIN CASE: bsc ACCEPTED block 2 with trigger tx %s (to=%s, gasPrice=0, coinbase-signed)", trigger.Hash().Hex(), tokenManager.Hex())
                t.Logf("block 1 carried %d auto-generated initContract system txs (positive control: the system-tx machinery itself works)", len(blocks[0].Transactions()))
        })

        // Stage 5 extension: the two REMAINING delta addresses of the F-1 set
        // divergence. Stage 4 runtime-proved 0x1008 (TokenManager); 0x2001 (Staking)
        // and 0x2003 (StakeCredit) were only supported by code-level set membership
        // (reth SYSTEM_CONTRACTS_SET). These subtests close that gap on the bsc side:
        // a coinbase-signed zero-gas tx to each address is a NORMAL transaction for
        // bsc as well, so blocks carrying them import cleanly. (The reth-bsc half is
        // covered by the Stage 5 binary-level devnet differential in GitHub Actions.)
        t.Run("Main_CoinbaseZeroGasTxToStaking_Accepted", func(t *testing.T) {
                f1AssertDeltaTargetAccepted(t, "Staking (0x2001)", common.HexToAddress(systemcontracts.StakingContract))
        })

        t.Run("Main_CoinbaseZeroGasTxToStakeCredit_Accepted", func(t *testing.T) {
                f1AssertDeltaTargetAccepted(t, "StakeCredit (0x2003)", common.HexToAddress(systemcontracts.StakeCreditContract))
        })

        // Control 1 — same shape but to ValidatorContract (0x2000), which IS in
        // bsc's systemContracts map: bsc must classify it as a system tx, and
        // since no system tx is generated for block 2, the leftover check in
        // Parlia.Finalize must reject the block. This proves the harness exercises
        // real system-tx classification (it is not a trivially-accepting stub).
        t.Run("Control_CoinbaseZeroGasTxToValidatorContract_Rejected", func(t *testing.T) {
                _, _, err := f1RunChain(t, func(signer types.Signer, gen *core.BlockGen) *types.Transaction {
                        key, _ := crypto.HexToECDSA(f1ValidatorKeyHex)
                        return f1SignedTx(signer, f1ValidatorKeyHex, gen.TxNonce(crypto.PubkeyToAddress(key.PublicKey)), validatorContract, new(big.Int))
                })
                if err == nil {
                        t.Fatalf("control case FAILED: bsc accepted a coinbase zero-gas tx to the ValidatorContract without a matching generated system tx; expected the systemTxs leftover check to reject it")
                }
                if !strings.Contains(err.Error(), "systemTxs") {
                        t.Fatalf("control case rejected for the wrong reason (want 'the length of systemTxs do not match'), got: %v", err)
                }
                t.Logf("CONTROL: bsc rejected the same tx shape to %s with: %v", validatorContract.Hex(), err)
        })

        // Control 2 — same tx but with a non-zero gas price: not classified as a
        // system tx by EITHER client, so bsc must accept (guards against the
        // misreading "any coinbase tx to 0x1008 breaks bsc").
        t.Run("Control_CoinbasePaidTxToTokenManager_Accepted", func(t *testing.T) {
                _, _, err := f1RunChain(t, func(signer types.Signer, gen *core.BlockGen) *types.Transaction {
                        key, _ := crypto.HexToECDSA(f1ValidatorKeyHex)
                        return f1SignedTx(signer, f1ValidatorKeyHex, gen.TxNonce(crypto.PubkeyToAddress(key.PublicKey)), tokenManager, big.NewInt(params.GWei))
                })
                if err != nil {
                        t.Fatalf("paid-gas control FAILED: bsc rejected a coinbase tx with 1 gwei gas price to 0x1008: %v", err)
                }
        })

        // Control 3 — zero-gas tx to 0x1008 from a non-coinbase sender: the
        // classification predicate requires sender == coinbase, so this is a
        // normal tx and both clients must accept.
        t.Run("Control_NonCoinbaseZeroGasTxToTokenManager_Accepted", func(t *testing.T) {
                _, _, err := f1RunChain(t, func(signer types.Signer, gen *core.BlockGen) *types.Transaction {
                        return f1SignedTx(signer, f1Sender2KeyHex, gen.TxNonce(f1Sender2), tokenManager, new(big.Int))
                })
                if err != nil {
                        t.Fatalf("non-coinbase control FAILED: bsc rejected a zero-gas tx from a plain sender to 0x1008: %v", err)
                }
        })
}

// f1AssertDeltaTargetAccepted runs the F-1 trigger shape against one of the
// reth-only system-contract targets (0x2001 Staking / 0x2003 StakeCredit) and
// asserts that bsc treats it as a NORMAL transaction: the block imports, the
// receipt is successful with plain-transfer gas semantics.
func f1AssertDeltaTargetAccepted(t *testing.T, label string, target common.Address) {
        t.Helper()
        var trigger *types.Transaction
        chain, blocks, err := f1RunChain(t, func(signer types.Signer, gen *core.BlockGen) *types.Transaction {
                key, _ := crypto.HexToECDSA(f1ValidatorKeyHex)
                trigger = f1SignedTx(signer, f1ValidatorKeyHex, gen.TxNonce(crypto.PubkeyToAddress(key.PublicKey)), target, new(big.Int))
                return trigger
        })
        if err != nil {
                t.Fatalf("F-1 delta case %s FAILED: bsc rejected the trigger block, but the hypothesis predicts acceptance: %v", label, err)
        }
        block2 := blocks[1]
        if got := len(block2.Transactions()); got != 1 {
                t.Fatalf("%s: block 2 should carry exactly the trigger tx, has %d", label, got)
        }
        tx := block2.Transactions()[0]
        if tx.Hash() != trigger.Hash() {
                t.Fatalf("%s: block 2 tx %v != trigger %v", label, tx.Hash(), trigger.Hash())
        }
        if tx.To() == nil || *tx.To() != target {
                t.Fatalf("%s: trigger tx to = %v, want %s", label, tx.To(), target.Hex())
        }
        if tx.GasPrice().Sign() != 0 {
                t.Fatalf("%s: trigger tx gas price = %v, want 0", label, tx.GasPrice())
        }
        from, err := types.Sender(types.LatestSigner(f1Config()), tx)
        if err != nil || from != block2.Coinbase() {
                t.Fatalf("%s: trigger sender %v (err %v) != coinbase %s", label, from, err, block2.Coinbase().Hex())
        }
        receipts := chain.GetReceiptsByHash(block2.Hash())
        if len(receipts) != 1 {
                t.Fatalf("%s: block 2 should have 1 receipt (normal execution), has %d", label, len(receipts))
        }
        if receipts[0].Status != types.ReceiptStatusSuccessful {
                t.Fatalf("%s: trigger receipt status = %d, want 1 (successful normal execution)", label, receipts[0].Status)
        }
        if receipts[0].GasUsed != params.TxGas {
                t.Fatalf("%s: trigger receipt gasUsed = %d, want %d (plain value-transfer semantics => normal classification)", label, receipts[0].GasUsed, params.TxGas)
        }
        if chain.GetBlockByNumber(2) == nil || chain.GetBlockByNumber(2).Hash() != block2.Hash() {
                t.Fatalf("%s: block 2 not canonical after import", label)
        }
        t.Logf("F-1 DELTA CASE %s: bsc ACCEPTED block 2 with trigger tx %s (to=%s, gasPrice=0, coinbase-signed)", label, trigger.Hash().Hex(), target.Hex())
}
