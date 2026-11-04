"""Unit tests: frame parsing, schema validation/mapping, version negotiation and diffs."""
import json
import unittest

from playground.protocol import V21, V201, schemas, select
from playground.protocol.frames import FrameError, MessageType, error_frame, parse


class FrameParsing(unittest.TestCase):
    def test_call(self):
        f = parse('[2,"abc","BootNotification",{"reason":"PowerUp"}]')
        self.assertEqual((f.type, f.message_id, f.action), (MessageType.CALL, "abc", "BootNotification"))

    def test_callresult_and_roundtrip(self):
        raw = '[3,"abc",{"currentTime":"2026-01-01T00:00:00Z"}]'
        self.assertEqual(parse(raw).to_json(), raw)

    def test_2_1_frame_types_parse(self):
        self.assertIs(parse('[5,"abc","FormatViolation","bad",{}]').type, MessageType.CALLRESULTERROR)
        self.assertIs(parse('[6,"abc","NotifyPeriodicEventStream",{}]').type, MessageType.SEND)

    def assertFrameError(self, raw, code, message_id):
        with self.assertRaises(FrameError) as ctx:
            parse(raw)
        self.assertEqual((ctx.exception.code, ctx.exception.message_id), (code, message_id))

    def test_not_json_has_unreadable_id(self):
        self.assertFrameError('[2, "x", "Heartbeat", {', "RpcFrameworkError", None)

    def test_non_string_id_is_unreadable(self):
        self.assertFrameError('[2, 123, "Heartbeat", {}]', "RpcFrameworkError", None)

    def test_id_longer_than_36(self):
        self.assertFrameError(json.dumps([2, "x" * 37, "Heartbeat", {}]), "RpcFrameworkError", "x" * 37)

    def test_unknown_message_type(self):
        self.assertFrameError('[7, "abc", "Heartbeat", {}]', "MessageTypeNotSupported", "abc")

    def test_wrong_element_count(self):
        self.assertFrameError('[2, "abc", "Heartbeat"]', "ProtocolError", "abc")

    def test_error_frame_uses_minus_one_when_id_unknown(self):
        self.assertEqual(json.loads(error_frame(None, "RpcFrameworkError").to_json())[1], "-1")
        self.assertEqual(json.loads(error_frame("a", "X", result_error=True).to_json())[0], 5)


class Schemas(unittest.TestCase):
    def setUp(self):
        self.s201 = schemas.store(V201)

    def code(self, action, payload):
        v = self.s201.validate(action, "request", payload)
        return v.code if v else None

    def test_valid_payload(self):
        self.assertIsNone(self.code("Heartbeat", {}))

    def test_keyword_mapping(self):
        sn = {"timestamp": "2026-01-01T00:00:00Z", "connectorStatus": "Available", "evseId": 1, "connectorId": 1}
        self.assertEqual(self.code("StatusNotification", {**sn, "connectorStatus": "Exploded"}), "PropertyConstraintViolation")
        self.assertEqual(self.code("StatusNotification", {**sn, "evseId": "1"}), "TypeConstraintViolation")
        self.assertEqual(self.code("StatusNotification", {k: v for k, v in sn.items() if k != "evseId"}),
                         "OccurrenceConstraintViolation")
        self.assertEqual(self.code("Heartbeat", {"extra": 1}), "FormatViolation")

    def test_send_actions_only_in_2_1(self):
        self.assertIn("NotifyPeriodicEventStream", schemas.store(V21).send_actions)
        self.assertEqual(self.s201.send_actions, set())

    def test_core_provisioning_messages_unchanged_in_2_1(self):
        for name in ("BootNotificationRequest", "BootNotificationResponse", "HeartbeatRequest",
                     "HeartbeatResponse", "StatusNotificationRequest"):
            self.assertTrue(schemas.is_unchanged(schemas.diff(self.s201, schemas.store(V21), name)), name)

    def test_transaction_event_changed_in_2_1(self):
        d = schemas.diff(self.s201, schemas.store(V21), "TransactionEventRequest")
        self.assertIn("costDetails", d["added"])
        self.assertTrue(any(e["field"] == "idToken.type" for e in d["enum_removed"]))


class Negotiation(unittest.TestCase):
    def test_server_preference_wins(self):
        self.assertIs(select(["ocpp2.0.1", "ocpp2.1"], ["2.1", "2.0.1"]), V21)
        self.assertIs(select(["ocpp2.0.1", "ocpp2.1"], ["2.0.1"]), V201)

    def test_no_common_version(self):
        self.assertIsNone(select(["ocpp1.6"], ["2.1", "2.0.1"]))


if __name__ == "__main__":
    unittest.main()
