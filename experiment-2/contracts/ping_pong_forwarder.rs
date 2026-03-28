// ============================================================================
// ping_pong_forwarder.rs
// MultiversX Smart Contract — Cross-Shard EGLD Ping-Pong Forwarder
//
// PURPOSE
// -------
// This contract is one node in a ring of 3 contracts, each deployed on a
// different shard. It receives EGLD and forwards it to a peer contract on
// the next shard, forming the ring:
//
//   shard0 → shard1 → shard2 → shard0 → ...
//
// Execution terminates when current_hop >= max_hops. This is NEVER infinite.
//
// CROSS-SHARD EXECUTION MODEL
// ---------------------------
// When this contract calls its peer on another shard, the MultiversX protocol
// routes the call via an asynchronous cross-shard SCR (Smart Contract Result).
// The execution sequence is:
//
//   1. This contract executes on its own shard (synchronously).
//   2. A SCR is created and placed into the cross-shard inbox of the peer shard.
//   3. The peer shard processes the SCR in a subsequent block (1–2 extra rounds).
//   4. The peer contract executes on its own shard.
//
// This means each cross-shard hop introduces ~2–6 seconds of latency depending
// on round time and shard load.
//
// WHY CALLBACKS ARE AVOIDED
// -------------------------
// Async callbacks in MultiversX require:
//   - reserving callback gas at the call site
//   - the callback executing on the CALLER'S shard
//   - complex state management across two execution contexts
//
// For this experiment, callbacks would add noise to latency measurements,
// consume additional gas, and complicate the hop-counting logic. Since we
// only need FORWARD progress (not response acknowledgement), we use a
// fire-and-forget transfer-execute pattern. Each hop is fully self-contained
// and observable via events.
//
// GAS IMPLICATIONS
// ----------------
// Each forward call must supply enough gas for:
//   - The peer contract's storage reads (set_peer storage read)
//   - The peer contract's forward() endpoint logic
//   - Event emission (~1000 gas per topic)
//   - The subsequent cross-shard SCR gas if not at max_hops
//
// If insufficient gas is forwarded, the cross-shard call will fail silently
// (from this contract's perspective). The hop chain will break.
// Recommended: forward at least 15_000_000 gas per hop.
//
// FRAMEWORK VERSION: multiversx-sc 0.53.x
// ============================================================================

#![no_std]

multiversx_sc::imports!();
multiversx_sc::derive_imports!();

#[type_abi]
#[derive(TopEncode)]
pub struct HopData<M: ManagedTypeApi> {
    pub max_hops: u64,
    pub sender: ManagedAddress<M>,
    pub contract_address: ManagedAddress<M>,
    pub next_peer: ManagedAddress<M>,
    pub amount: BigUint<M>,
}

/// Minimum EGLD amount required to forward (1 microEGLD = 10^12 attoEGLD).
/// This prevents dust attacks and ensures gas fees can be covered by the
/// forwarded amount on each hop.
const MIN_EGLD_AMOUNT: u64 = 1_000_000_000_000; // 0.000001 EGLD

/// Maximum allowed hops to prevent accidental infinite chains.
/// Even if the caller passes max_hops = 1000, we cap at this value.
const ABSOLUTE_MAX_HOPS: u64 = 200;

/// Gas we keep for our own execution overhead (events, storage reads).
/// We subtract this from caller-supplied gas before forwarding.
const GAS_RESERVE_FOR_SELF: u64 = 3_000_000;

#[multiversx_sc::contract]
pub trait PingPongForwarder {
    // =========================================================================
    // INIT
    // =========================================================================

    /// Initialize the contract with the address of the peer contract
    /// on the NEXT shard in the ring.
    ///
    /// peer_address: bech32 or hex address of the next contract
    #[init]
    fn init(&self, peer_address: ManagedAddress) {
        require!(
            !peer_address.is_zero(),
            "peer_address cannot be zero"
        );
        self.peer_address().set(&peer_address);
    }

    // =========================================================================
    // UPGRADE
    // =========================================================================

    #[upgrade]
    fn upgrade(&self) {}

    // =========================================================================
    // OWNER ENDPOINT: set_peer
    // =========================================================================

    /// Update the peer address after deployment.
    /// This is needed during the init phase to wire up the ring topology
    /// AFTER all 3 contracts are deployed (since we don't know future
    /// addresses at deploy time).
    ///
    /// Only callable by the contract owner.
    #[only_owner]
    #[endpoint(setPeer)]
    fn set_peer(&self, peer_address: ManagedAddress) {
        require!(
            !peer_address.is_zero(),
            "peer_address cannot be zero"
        );
        self.peer_address().set(&peer_address);

        self.peer_updated_event(
            &peer_address,
            &self.blockchain().get_caller(),
        );
    }

    // =========================================================================
    // PUBLIC ENDPOINT: start_ping_pong
    // =========================================================================

    /// Entry point for a new ping-pong chain.
    ///
    /// This endpoint:
    ///   1. Validates inputs (correlation_id, max_hops, payment)
    ///   2. Emits a "started" event
    ///   3. Delegates to the internal forward logic at hop=0
    ///
    /// # Arguments
    /// - correlation_id: unique hex string identifying this execution chain
    /// - max_hops: number of cross-shard hops before stopping (capped at 200)
    ///
    /// # Payment
    /// Must send EGLD >= 0.000001 EGLD (MIN_EGLD_AMOUNT)
    #[payable("EGLD")]
    #[endpoint(startPingPong)]
    fn start_ping_pong(
        &self,
        correlation_id: ManagedBuffer,
        max_hops: u64,
    ) {
        let amount = self.call_value().egld().clone_value();
        let caller = self.blockchain().get_caller();
        let peer = self.peer_address().get();
        let self_address = self.blockchain().get_sc_address();

        // ── Validate inputs ──────────────────────────────────────────────────

        require!(
            !correlation_id.is_empty(),
            "correlation_id cannot be empty"
        );
        require!(
            max_hops > 0,
            "max_hops must be > 0"
        );
        require!(
            max_hops <= ABSOLUTE_MAX_HOPS,
            "max_hops exceeds ABSOLUTE_MAX_HOPS (200)"
        );
        require!(
            !peer.is_zero(),
            "peer address not configured — call setPeer first"
        );
        require!(
            amount >= BigUint::from(MIN_EGLD_AMOUNT),
            "payment too small — minimum 0.000001 EGLD"
        );

        // ── Emit start event ─────────────────────────────────────────────────

        self.ping_pong_event(
            &correlation_id,
            0u64,
            &ManagedBuffer::from(b"started"),
            &HopData {
                max_hops,
                sender: caller,
                contract_address: self_address,
                next_peer: peer.clone(),
                amount: amount.clone(),
            },
        );

        // ── Forward to peer at hop=1 ─────────────────────────────────────────

        // We use transfer_execute (fire-and-forget cross-shard call).
        // Gas forwarded = available_gas - GAS_RESERVE_FOR_SELF.
        // The peer will further reduce on each subsequent hop.
        let gas_for_peer = self.blockchain().get_gas_left()
            .saturating_sub(GAS_RESERVE_FOR_SELF);

        let mut call = self.send()
            .contract_call::<()>(peer.clone(), ManagedBuffer::from(b"forward"))
            .with_egld_transfer(amount.clone())
            .with_gas_limit(gas_for_peer);
        call.push_raw_argument(correlation_id.clone());
        call.push_raw_argument(self.encode_u64(1u64));
        call.push_raw_argument(self.encode_u64(max_hops));
        call.transfer_execute();
    }

    // =========================================================================
    // PUBLIC ENDPOINT: forward
    // =========================================================================

    /// Core forwarding endpoint. Called by the PREVIOUS contract in the ring.
    ///
    /// Behavior:
    ///   - If current_hop >= max_hops → STOP, emit "stopped" event, keep EGLD
    ///   - Otherwise → emit "forwarded" event, send EGLD to peer at hop+1
    ///
    /// # Arguments
    /// - correlation_id: same as start_ping_pong
    /// - current_hop: the hop index that ARRIVES at this contract
    /// - max_hops: total allowed hops (carried through the chain)
    ///
    /// # Payment
    /// Must receive EGLD (forwarded from the previous contract)
    ///
    /// # Cross-shard note
    /// When this function calls self.send().contract_call().transfer_execute(),
    /// the MultiversX protocol creates a SCR (SmartContractResult) destined
    /// for the peer shard. That SCR will be processed in the peer shard's
    /// next block. There is NO synchronous return value across shards.
    #[payable("EGLD")]
    #[endpoint(forward)]
    fn forward(
        &self,
        correlation_id: ManagedBuffer,
        current_hop: u64,
        max_hops: u64,
    ) {
        let amount = self.call_value().egld().clone_value();
        let caller = self.blockchain().get_caller();
        let peer = self.peer_address().get();
        let self_address = self.blockchain().get_sc_address();

        // ── Safety guards ─────────────────────────────────────────────────────

        require!(
            !correlation_id.is_empty(),
            "correlation_id cannot be empty"
        );
        require!(
            max_hops > 0 && max_hops <= ABSOLUTE_MAX_HOPS,
            "invalid max_hops"
        );
        require!(
            amount >= BigUint::from(MIN_EGLD_AMOUNT),
            "payment too small"
        );

        // ── STOP condition ───────────────────────────────────────────────────

        if current_hop >= max_hops {
            // We have reached the hop limit.
            // Keep the EGLD in this contract (or it could be returned to
            // the chain initiator — for simplicity we keep it here).
            // Emit a "stopped" event so the Python runner can detect termination.
            self.ping_pong_event(
                &correlation_id,
                current_hop,
                &ManagedBuffer::from(b"stopped"),
                &HopData {
                    max_hops,
                    sender: caller,
                    contract_address: self_address,
                    next_peer: peer.clone(),
                    amount: amount.clone(),
                },
            );
            return;
        }

        // ── FORWARD condition ─────────────────────────────────────────────────

        self.ping_pong_event(
            &correlation_id,
            current_hop,
            &ManagedBuffer::from(b"forwarded"),
            &HopData {
                max_hops,
                sender: caller,
                contract_address: self_address,
                next_peer: peer.clone(),
                amount: amount.clone(),
            },
        );

        // Decrement available gas to leave room for our own epilogue.
        let gas_for_peer = self.blockchain().get_gas_left()
            .saturating_sub(GAS_RESERVE_FOR_SELF);

        let next_hop = current_hop + 1u64;

        // Fire-and-forget cross-shard transfer-execute.
        // The MultiversX protocol guarantees atomicity at the originating
        // shard level: if this line fails, the entire forward() call is
        // rolled back (EGLD stays here). However, once the SCR is placed in
        // the cross-shard inbox, its execution on the peer shard is
        // independent.
        let mut call = self.send()
            .contract_call::<()>(peer.clone(), ManagedBuffer::from(b"forward"))
            .with_egld_transfer(amount.clone())
            .with_gas_limit(gas_for_peer);
        call.push_raw_argument(correlation_id.clone());
        call.push_raw_argument(self.encode_u64(next_hop));
        call.push_raw_argument(self.encode_u64(max_hops));
        call.transfer_execute();
    }

    // =========================================================================
    // VIEWS
    // =========================================================================

    /// Returns the currently configured peer address.
    #[view(getPeer)]
    fn get_peer(&self) -> ManagedAddress {
        self.peer_address().get()
    }

    // =========================================================================
    // EVENTS
    // =========================================================================

    /// Main event emitted on every hop (started / forwarded / stopped).
    ///
    /// Topics (indexed, searchable via API):
    ///   1. correlation_id — unique chain identifier
    ///   2. status         — "started" | "forwarded" | "stopped"
    ///
    /// Data (non-indexed, included in event data blob):
    ///   - hop_index, max_hops, sender, contract_address, next_peer, amount
    ///
    /// The Python runner queries these events via the MultiversX API to
    /// reconstruct the full hop timeline.
    #[event("pingPongHop")]
    fn ping_pong_event(
        &self,
        #[indexed] correlation_id: &ManagedBuffer,
        #[indexed] hop_index: u64,
        #[indexed] status: &ManagedBuffer,
        data: &HopData<Self::Api>,
    );

    /// Emitted when the owner updates the peer address.
    #[event("peerUpdated")]
    fn peer_updated_event(
        &self,
        #[indexed] new_peer: &ManagedAddress,
        caller: &ManagedAddress,
    );

    // =========================================================================
    // STORAGE
    // =========================================================================

    /// The address of the next contract in the ring (on the next shard).
    #[storage_mapper("peer_address")]
    fn peer_address(&self) -> SingleValueMapper<ManagedAddress>;

    // =========================================================================
    // PRIVATE HELPERS
    // =========================================================================

    /// Encode a u64 as a big-endian hex ManagedBuffer for use as a raw
    /// ABI argument in transfer_execute calls.
    ///
    /// MultiversX ABI encodes u64 as big-endian bytes with leading zeros
    /// stripped (minimum 1 byte). We replicate this encoding here.
    fn encode_u64(&self, value: u64) -> ManagedBuffer {
        let bytes = value.to_be_bytes();
        // Find first non-zero byte
        let mut start = 0usize;
        while start < 7 && bytes[start] == 0 {
            start += 1;
        }
        ManagedBuffer::from(&bytes[start..])
    }
}
