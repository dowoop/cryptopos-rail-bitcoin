"""Hostile transport and provider-shape tests for complete payment rails."""

import json
import unittest
from unittest import mock

import cryptopos_rail_bitcoin as bitcoin
from cryptopos_core.errors import AddressRefused, InvalidRailPlugin, RailProviderError
from cryptopos_core.plugin import (
	NEEDS_REVIEW,
	PENDING,
	ObservationBatch,
	PaymentIntent,
	RecipientBaseline,
	TransferObservation,
)

BTC_KEY = bitcoin.BitcoinTestnet4.key
ENDPOINT = "https://provider.example/api"
BTC_ADDRESS = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"
BTC_TX = "a" * 64
EVM_RECIPIENT = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
EVM_TX = "0x" + "a" * 64
EVM_BLOCK = "0x" + "1" * 64


class RawGet:
	def __init__(self, value):
		self.value = value

	def get(self, url, timeout, max_bytes):
		if isinstance(self.value, Exception):
			raise self.value
		return self.value


class RawPost:
	def __init__(self, value):
		self.value = value

	def post(self, url, body, timeout, max_bytes):
		if isinstance(self.value, Exception):
			raise self.value
		return self.value


class RpcHandler:
	def __init__(self, handler):
		self.handler = handler

	def post(self, url, body, timeout, max_bytes):
		request = json.loads(body)
		result = self.handler(request["method"], request["params"])
		return json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()


class Response:
	def __init__(self, body, content_length=None):
		self.body = body
		self.read_limit = None
		self.headers = {}
		if content_length is not None:
			self.headers["Content-Length"] = content_length

	def __enter__(self):
		return self

	def __exit__(self, *ignored):
		return False

	def read(self, limit):
		self.read_limit = limit
		return self.body


class Opener:
	def __init__(self, response):
		self.response = response
		self.requests = []

	def open(self, request, timeout):
		self.requests.append((request, timeout))
		return self.response


class BitcoinTransportFailures(unittest.TestCase):
	def test_redirects_are_never_followed(self):
		self.assertIsNone(bitcoin._NoRedirect().redirect_request(None, None, 302, "moved", {}, ENDPOINT))

	def test_default_transport_builds_a_bounded_get(self):
		opener = Opener(Response(b"abc", "3"))
		with mock.patch("urllib.request.build_opener", return_value=opener) as build_opener:
			transport = bitcoin._HttpsTransport()
			self.assertEqual(transport.get(ENDPOINT, 2, 3), b"abc")
		handlers = build_opener.call_args.args
		self.assertIs(handlers[0], bitcoin._NoRedirect)
		self.assertEqual(handlers[1].proxies, {})
		request, timeout = opener.requests[0]
		self.assertEqual((request.method, request.full_url, timeout), ("GET", ENDPOINT, 2))
		self.assertEqual(
			request.get_header("User-agent"),
			f"cryptopos-rail-bitcoin/{bitcoin.__version__}",
		)
		self.assertEqual(opener.response.read_limit, 4)

	def test_default_transport_accepts_an_explicit_zero_length_body(self):
		transport = bitcoin._HttpsTransport.__new__(bitcoin._HttpsTransport)
		transport._opener = Opener(Response(b"", "0"))
		self.assertEqual(transport.get(ENDPOINT, 2, 3), b"")

	def test_default_transport_refuses_declared_or_actual_oversize_bodies(self):
		for response in (
			Response(b"", "not-a-number"),
			Response(b"", "-1"),
			Response(b"", "4"),
			Response(b"abcd"),
		):
			with self.subTest(response=response):
				transport = bitcoin._HttpsTransport.__new__(bitcoin._HttpsTransport)
				transport._opener = Opener(response)
				with self.assertRaises(ValueError):
					transport.get(ENDPOINT, 2, 3)

	def test_configuration_rejects_unsafe_shapes(self):
		for configuration in (
			None,
			{},
			{"endpoint": "http://provider.example"},
			{"endpoint": "https://user:secret@provider.example"},
			{"endpoint": "https://provider.example?network=mainnet"},
			{"endpoint": ENDPOINT, "transport": object()},
			{"endpoint": ENDPOINT, "transport": RawGet(b""), "timeout_seconds": 0},
		):
			with self.subTest(configuration=configuration), self.assertRaises(RailProviderError):
				bitcoin._configuration(configuration)
		with mock.patch.object(bitcoin, "_HttpsTransport", return_value=RawGet(b"")) as factory:
			base, transport, timeout = bitcoin._configuration({"endpoint": ENDPOINT + "/"})
		self.assertEqual((base, timeout), (ENDPOINT, bitcoin.DEFAULT_TIMEOUT_SECONDS))
		self.assertIs(transport, factory.return_value)
		self.assertEqual(
			bitcoin._configuration({"endpoint": ENDPOINT, "transport": RawGet(b""), "timeout_seconds": 30})[
				2
			],
			30.0,
		)
		self.assertEqual(
			bitcoin._configuration({"endpoint": ENDPOINT, "transport": RawGet(b""), "timeout_seconds": 1})[2],
			1.0,
		)
		with self.assertRaises(RailProviderError):
			bitcoin._configuration(
				{"endpoint": ENDPOINT, "transport": RawGet(b""), "timeout_seconds": 30.0001}
			)

	def test_read_normalizes_transport_failures_and_bounds(self):
		provider_error = RailProviderError(BTC_KEY, "specific")
		for value, phrase in (
			(provider_error, "specific"),
			(ValueError("bad wire"), "failed"),
			("not bytes", "non-byte"),
			(b"x" * (bitcoin.MAX_RESPONSE_BYTES + 1), "safety limit"),
		):
			with self.subTest(value=value), self.assertRaises(RailProviderError) as caught:
				bitcoin._read(ENDPOINT, RawGet(value), 2, "/status")
			self.assertIn(phrase, caught.exception.reason)

	def test_text_and_json_refuse_bad_encodings_and_documents(self):
		for function, payload in (
			(bitcoin._text, b"\xff"),
			(bitcoin._json, b"\xff"),
			(bitcoin._json, b"{"),
		):
			with self.subTest(function=function, payload=payload), self.assertRaises(RailProviderError):
				function(ENDPOINT, RawGet(payload), 2, "/data")

	def test_tip_and_transaction_page_are_strictly_bounded(self):
		with self.assertRaises(RailProviderError):
			bitcoin._tip(ENDPOINT, RawGet(b"-1"), 2)
		for payload in ({"not": "a list"}, [{}] * (bitcoin.MAX_TRANSACTIONS + 1)):
			with self.subTest(payload=payload), self.assertRaises(RailProviderError):
				bitcoin._transactions(ENDPOINT, RawGet(json.dumps(payload).encode()), 2, "recipient")
		self.assertEqual(bitcoin._exact_nonnegative(0, "field"), 0)
		self.assertEqual(
			len(
				bitcoin._transactions(
					ENDPOINT,
					RawGet(json.dumps([{}] * bitcoin.MAX_TRANSACTIONS).encode()),
					2,
					"recipient",
				)
			),
			bitcoin.MAX_TRANSACTIONS,
		)

	def test_read_accepts_a_body_exactly_at_the_limit(self):
		body = b"x" * bitcoin.MAX_RESPONSE_BYTES
		self.assertIs(bitcoin._read(ENDPOINT, RawGet(body), 2, "/body"), body)

	def test_safety_constants_are_pinned(self):
		self.assertEqual(bitcoin.MAX_RESPONSE_BYTES, 2_000_000)
		self.assertEqual(bitcoin.MAX_TRANSACTIONS, 50)
		self.assertEqual(bitcoin.MAX_TRANSACTION_INPUTS, 10_000)
		self.assertEqual(bitcoin.MAX_TRANSACTION_OUTPUTS, 10_000)
		self.assertEqual(bitcoin.BitcoinTestnet4.asset.decimals, 8)


class BitcoinProviderDataFailures(unittest.TestCase):
	def setUp(self):
		self.rail = bitcoin.BitcoinTestnet4()
		self.baseline = RecipientBaseline(self.rail.key, BTC_ADDRESS, ENDPOINT, 5)
		self.intent = PaymentIntent(
			"sale-1", self.rail.key, BTC_ADDRESS, 10, 100, 200, baseline=self.baseline
		)

	def transaction(self, **changes):
		value = {
			"txid": BTC_TX,
			"vin": [],
			"vout": [{"scriptpubkey_address": BTC_ADDRESS, "value": 10}],
			"status": {"confirmed": True, "block_height": 6, "block_time": 150},
		}
		value.update(changes)
		return value

	def test_request_and_observation_require_valid_bound_intents(self):
		bad_baseline = RecipientBaseline(self.rail.key, "bad", ENDPOINT, 5)
		bad_intent = PaymentIntent("sale", self.rail.key, "bad", 10, 100, 200, baseline=bad_baseline)
		with self.assertRaises(AddressRefused):
			self.rail.create_request(bad_intent)
		without_baseline = PaymentIntent("sale", self.rail.key, BTC_ADDRESS, 10, 100, 200)
		with self.assertRaises(InvalidRailPlugin):
			self.rail.observe(without_baseline, {})
		with self.assertRaises(InvalidRailPlugin):
			self.rail._intent(object())

	def test_observation_revalidates_previous_binding_and_monotonic_tip(self):
		previous = ObservationBatch(
			self.rail.key,
			self.intent.intent_id,
			BTC_ADDRESS,
			ENDPOINT,
			5,
			5,
			5,
			5,
			(),
		)
		with (
			mock.patch.object(bitcoin, "_verified_provider", return_value=(ENDPOINT, object(), 2)),
			mock.patch.object(bitcoin, "_tip", return_value=5),
			mock.patch.object(bitcoin, "_transactions", return_value=[]),
		):
			observed = self.rail.observe(self.intent, {}, previous)
		self.assertTrue(observed.complete)
		with (
			mock.patch.object(bitcoin, "_verified_provider", return_value=(ENDPOINT, object(), 2)),
			mock.patch.object(bitcoin, "_tip", return_value=4),
		):
			with self.assertRaises(RailProviderError):
				self.rail.observe(self.intent, {})

	def test_settlement_refuses_unknown_incomplete_or_malformed_claims(self):
		complete = ObservationBatch(self.rail.key, "sale-1", BTC_ADDRESS, ENDPOINT, 5, 6, 5, 6, ())
		incomplete = ObservationBatch(self.rail.key, "sale-1", BTC_ADDRESS, ENDPOINT, 5, 7, 5, 6, ())
		with self.assertRaises(InvalidRailPlugin):
			self.rail.settle(self.intent, object())
		with self.assertRaises(InvalidRailPlugin):
			self.rail.settle(self.intent, incomplete)
		with self.assertRaises(InvalidRailPlugin):
			self.rail.settle(self.intent, complete, {BTC_TX})

	def test_settlement_reports_overpayment_and_confirmed_underpayment(self):
		overpaid = ObservationBatch(
			self.rail.key,
			"sale-1",
			BTC_ADDRESS,
			ENDPOINT,
			5,
			6,
			5,
			6,
			(TransferObservation(BTC_TX, 11, True, 1, 6, 150),),
		)
		decision = self.rail.settle(self.intent, overpaid)
		self.assertIn("exceeds", decision.reason)
		underpaid = ObservationBatch(
			self.rail.key,
			"sale-1",
			BTC_ADDRESS,
			ENDPOINT,
			5,
			6,
			5,
			6,
			(TransferObservation(BTC_TX, 9, True, 1, 6, 150),),
		)
		decision = self.rail.settle(self.intent, underpaid)
		self.assertEqual(decision.state, PENDING)
		self.assertIn("below", decision.reason)

	def test_settlement_pins_expiry_overpayment_claim_and_late_boundaries(self):
		def observed(amount, *, block_time=150, transaction_id=BTC_TX):
			return ObservationBatch(
				self.rail.key,
				"sale-1",
				BTC_ADDRESS,
				ENDPOINT,
				5,
				6,
				5,
				6,
				(TransferObservation(transaction_id, amount, True, 1, 6, block_time),),
			)

		exact = self.rail.settle(self.intent, observed(10, block_time=200))
		self.assertEqual(exact.state, "settled")
		self.assertNotIn("exceeds", exact.reason)
		claimed = ObservationBatch(
			self.rail.key,
			"sale-1",
			BTC_ADDRESS,
			ENDPOINT,
			5,
			6,
			5,
			6,
			(
				TransferObservation(BTC_TX, 6, True, 1, 6, 150),
				TransferObservation("b" * 64, 5, True, 1, 6, 150),
			),
		)
		self.assertEqual(self.rail.settle(self.intent, claimed, frozenset({BTC_TX})).state, NEEDS_REVIEW)
		self.assertEqual(self.rail.settle(self.intent, observed(9, block_time=201)).state, PENDING)
		self.assertEqual(self.rail.settle(self.intent, observed(11, block_time=201)).state, NEEDS_REVIEW)

	def test_parser_skips_duplicates_baseline_transactions_and_zero_outputs(self):
		zero = self.transaction(vout=[{"scriptpubkey_address": "other", "value": 10}])
		self.assertEqual(self.rail._parse_transfers(self.intent, 6, [zero, zero]), [])
		bound = PaymentIntent(
			"sale-1",
			self.rail.key,
			BTC_ADDRESS,
			10,
			100,
			200,
			baseline=RecipientBaseline(self.rail.key, BTC_ADDRESS, ENDPOINT, 5, (BTC_TX,)),
		)
		self.assertEqual(self.rail._parse_transfers(bound, 6, [self.transaction()]), [])

	def test_parser_rejects_malformed_transaction_collections(self):
		cases = (
			self.transaction(vout={}),
			self.transaction(vout=[{}] * (bitcoin.MAX_TRANSACTION_OUTPUTS + 1)),
			self.transaction(vin={}),
			self.transaction(vin=[{}] * (bitcoin.MAX_TRANSACTION_INPUTS + 1)),
			self.transaction(vout=["output"]),
			self.transaction(vout=[{"scriptpubkey_address": 1, "value": 10}]),
			self.transaction(status={"confirmed": "yes"}),
			self.transaction(status={"confirmed": True, "block_height": 7, "block_time": 150}),
		)
		for transaction in cases:
			with self.subTest(transaction=transaction), self.assertRaises(RailProviderError):
				self.rail._parse_transfers(self.intent, 6, [transaction])
		with self.assertRaises(RailProviderError):
			self.rail._parse_transfers(self.intent, 6, [self.transaction(status="bad")])

	def test_parser_accepts_collection_ceilings_and_computes_exact_confirmations(self):
		outputs = [{"scriptpubkey_address": "other", "value": 0}] * (bitcoin.MAX_TRANSACTION_OUTPUTS - 1) + [
			{"scriptpubkey_address": BTC_ADDRESS, "value": 10}
		]
		inputs = [{"prevout": None}] * bitcoin.MAX_TRANSACTION_INPUTS
		parsed = self.rail._parse_transfers(self.intent, 6, [self.transaction(vout=outputs, vin=inputs)])
		self.assertEqual(parsed[0].confirmations, 1)

	def test_input_parser_refuses_malformed_prevouts_but_allows_coinbase(self):
		self.assertIs(self.rail._spends_from_recipient([{"prevout": None}], BTC_ADDRESS), False)
		for inputs in (
			["input"],
			[{"prevout": "bad"}],
			[{"prevout": {"scriptpubkey_address": 1}}],
		):
			with self.subTest(inputs=inputs), self.assertRaises(RailProviderError):
				self.rail._spends_from_recipient(inputs, BTC_ADDRESS)

