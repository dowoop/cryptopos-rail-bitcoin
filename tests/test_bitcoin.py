"""Bitcoin Testnet 4 is the reference complete installable rail."""

import json
import unittest

from cryptopos_rail_bitcoin import (
	MAX_RESPONSE_BYTES,
	TESTNET4_GENESIS_HASH,
	BitcoinTestnet4,
)
from cryptopos_core.errors import AddressRefused, InvalidRailPlugin, RailProviderError
from cryptopos_core.plugin import NEEDS_REVIEW, PENDING, SETTLED, PaymentIntent

ADDRESS = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"
ENDPOINT = "https://esplora.example/api"
TX_A = "a" * 64
TX_B = "b" * 64
TX_C = "c" * 64


class FakeTransport:
	def __init__(self, routes):
		self.routes = routes
		self.calls = []

	def get(self, url, timeout, max_bytes):
		self.calls.append((url, timeout, max_bytes))
		path = "/" + url.split("/api/", 1)[1]
		value = self.routes[path]
		if isinstance(value, Exception):
			raise value
		if isinstance(value, bytes):
			return value
		if isinstance(value, str):
			return value.encode()
		return json.dumps(value).encode()


def output(address, value):
	return {"scriptpubkey_address": address, "value": value}


def transaction(txid, value, *, confirmed=False, height=None, block_time=None, inputs=()):
	status = {"confirmed": confirmed}
	if confirmed:
		status.update({"block_height": height, "block_time": block_time})
	return {
		"txid": txid,
		"vin": list(inputs),
		"vout": [output(ADDRESS, value)],
		"status": status,
	}


def routes(tip=100, transactions=()):
	return {
		"/block-height/0": TESTNET4_GENESIS_HASH,
		"/blocks/tip/height": str(tip),
		f"/address/{ADDRESS}/txs": list(transactions),
	}


class BitcoinRailTest(unittest.TestCase):
	def setUp(self):
		self.rail = BitcoinTestnet4()

	def config(self, transport):
		return {"endpoint": ENDPOINT, "transport": transport, "timeout_seconds": 2}

	def baseline(self, tip=100):
		transport = FakeTransport(routes(tip))
		return self.rail.capture_baseline(ADDRESS, self.config(transport))

	def intent(self, baseline=None, amount=1500):
		return PaymentIntent(
			"sale-1",
			self.rail.key,
			ADDRESS,
			amount,
			1_000,
			2_000,
			baseline=baseline,
		)

	def test_concrete_identity_cannot_be_confused_with_generic_testnet(self):
		self.assertEqual(self.rail.key, "bitcoin:testnet4/native:btc")
		self.assertTrue(self.rail.network.is_testnet)
		self.assertEqual(self.rail.asset.symbol, "TBTC")

	def test_readiness_proves_genesis_and_tip(self):
		transport = FakeTransport(routes(101))
		readiness = self.rail.readiness(self.config(transport))
		self.assertTrue(readiness.chargeable)
		self.assertEqual(
			[url.removeprefix(ENDPOINT) for url, _timeout, _limit in transport.calls],
			["/block-height/0", "/blocks/tip/height"],
		)
		self.assertTrue(all(limit == MAX_RESPONSE_BYTES for _url, _timeout, limit in transport.calls))

	def test_readiness_refuses_an_unproven_or_insecure_provider(self):
		wrong = FakeTransport(routes())
		wrong.routes["/block-height/0"] = "0" * 64
		readiness = self.rail.readiness(self.config(wrong))
		self.assertFalse(readiness.chargeable)
		self.assertIn("not Bitcoin Testnet 4", readiness.reason_for("observation"))
		insecure = self.rail.readiness({"endpoint": "http://esplora.example"})
		self.assertFalse(insecure.chargeable)
		self.assertIn("HTTPS", insecure.reason_for("observation"))

	def test_baseline_requires_a_checksum_valid_fresh_address(self):
		with self.assertRaises(AddressRefused):
			self.rail.capture_baseline("not-an-address", self.config(FakeTransport(routes())))
		used = FakeTransport(routes(transactions=[transaction(TX_A, 1)]))
		with self.assertRaises(RailProviderError) as caught:
			self.rail.capture_baseline(ADDRESS, self.config(used))
		self.assertIn("fresh address", caught.exception.reason)

	def test_request_requires_the_pre_payment_baseline(self):
		with self.assertRaises(InvalidRailPlugin):
			self.rail.create_request(self.intent())
		request = self.rail.create_request(self.intent(self.baseline()))
		self.assertEqual(request.uri, f"bitcoin:{ADDRESS}?amount=0.00001500")
		self.assertEqual(request.rail_key, self.rail.key)

	def test_mempool_is_sighted_but_never_settled(self):
		baseline = self.baseline()
		transport = FakeTransport(routes(100, [transaction(TX_A, 1500)]))
		observations = self.rail.observe(self.intent(baseline), self.config(transport))
		decision = self.rail.settle(self.intent(baseline), observations)
		self.assertEqual(decision.state, PENDING)
		self.assertEqual(decision.sighted_native, 1500)
		self.assertEqual(decision.credited_native, 0)

	def test_one_confirmation_settles_and_split_payments_are_summed(self):
		baseline = self.baseline()
		paid = [
			transaction(TX_A, 900, confirmed=True, height=101, block_time=1_100),
			transaction(TX_B, 600, confirmed=True, height=102, block_time=1_200),
		]
		observations = self.rail.observe(self.intent(baseline), self.config(FakeTransport(routes(102, paid))))
		decision = self.rail.settle(self.intent(baseline), observations)
		self.assertEqual(decision.state, SETTLED)
		self.assertEqual(decision.credited_native, 1500)
		self.assertEqual(decision.transaction_id, TX_A)
		self.assertEqual(decision.transaction_ids, (TX_A, TX_B))

	def test_payment_after_expiry_needs_review(self):
		baseline = self.baseline()
		paid = transaction(TX_A, 1500, confirmed=True, height=101, block_time=2_001)
		observations = self.rail.observe(
			self.intent(baseline), self.config(FakeTransport(routes(101, [paid])))
		)
		decision = self.rail.settle(self.intent(baseline), observations)
		self.assertEqual(decision.state, NEEDS_REVIEW)
		self.assertIn("expiry", decision.reason)

	def test_observations_cannot_be_reused_for_another_intent(self):
		baseline = self.baseline()
		paid = transaction(TX_A, 1500, confirmed=True, height=101, block_time=1_100)
		first = self.intent(baseline)
		observations = self.rail.observe(first, self.config(FakeTransport(routes(101, [paid]))))
		other = PaymentIntent("sale-2", self.rail.key, ADDRESS, 1500, 1_000, 2_000, baseline=baseline)
		with self.assertRaises(InvalidRailPlugin):
			self.rail.settle(other, observations)

	def test_prebaseline_blocks_and_our_own_change_are_not_payments(self):
		baseline = self.baseline(100)
		old = transaction(TX_A, 2000, confirmed=True, height=100, block_time=900)
		change = transaction(
			TX_B,
			2000,
			confirmed=True,
			height=101,
			block_time=1_100,
			inputs=[{"prevout": {"scriptpubkey_address": ADDRESS}}],
		)
		observations = self.rail.observe(
			self.intent(baseline), self.config(FakeTransport(routes(101, [old, change])))
		)
		self.assertEqual(observations.transfers, ())

	def test_claimed_transaction_cannot_settle_a_second_intent(self):
		baseline = self.baseline()
		paid = transaction(TX_A, 1500, confirmed=True, height=101, block_time=1_100)
		observations = self.rail.observe(
			self.intent(baseline), self.config(FakeTransport(routes(101, [paid])))
		)
		decision = self.rail.settle(self.intent(baseline), observations, frozenset({TX_A}))
		self.assertEqual(decision.state, NEEDS_REVIEW)

	def test_provider_switch_after_baseline_is_refused(self):
		baseline = self.baseline()
		configuration = {
			"endpoint": "https://other.example/api",
			"transport": FakeTransport(routes()),
		}
		with self.assertRaises(RailProviderError) as caught:
			self.rail.observe(self.intent(baseline), configuration)
		self.assertIn("differs", caught.exception.reason)

	def test_hostile_provider_shapes_are_documented_provider_errors(self):
		baseline = self.baseline()
		for malformed in (
			{"not": "a list"},
			[transaction(TX_C, True)],
			[{"txid": "short", "vin": [], "vout": [], "status": {"confirmed": False}}],
		):
			with self.subTest(malformed=malformed):
				transport = FakeTransport(routes(101, malformed))
				with self.assertRaises(RailProviderError):
					self.rail.observe(self.intent(baseline), self.config(transport))


if __name__ == "__main__":
	unittest.main()
