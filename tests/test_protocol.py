"""Unit tests: frame parsing, per-version error codes, schema validation/mapping, negotiation, comparisons."""
import json
import unittest

from playground.protocol import V16, V21, V201, schemas, select
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
        self.assertFrameError('[2, "x", "Heartbeat", {', "rpc", None)

    def test_non_string_id_is_unreadable(self):
        self.assertFrameError('[2, 123, "Heartbeat", {}]', "rpc", None)

    def test_id_longer_than_36(self):
        self.assertFrameError(json.dumps([2, "x" * 37, "Heartbeat", {}]), "rpc", "x" * 37)

    def test_unknown_message_type(self):
        self.assertFrameError('[7, "abc", "Heartbeat", {}]', "unknown_type", "abc")

    def test_wrong_element_count(self):
        self.assertFrameError('[2, "abc", "Heartbeat"]', "protocol", "abc")

    def test_error_frame_uses_minus_one_when_id_unknown(self):
        self.assertEqual(json.loads(error_frame(None, "RpcFrameworkError").to_json())[1], "-1")
        self.assertEqual(json.loads(error_frame("a", "X", result_error=True).to_json())[0], 5)


class ErrorCodes(unittest.TestCase):
    def test_1_6_spellings(self):
        self.assertEqual(V16.code("format"), "FormationViolation")
        self.assertEqual(V16.code("occurrence"), "OccurenceConstraintViolation")
        self.assertIsNone(V16.code("unknown_type"))

    def test_2_x_names(self):
        for v in (V201, V21):
            self.assertEqual(v.code("format"), "FormatViolation")
            self.assertEqual(v.code("rpc"), "RpcFrameworkError")


class Schemas(unittest.TestCase):
    def code(self, version, action, payload):
        v = schemas.store(version).validate(action, "request", payload)
        return v.code if v else None

    def test_valid_payload(self):
        self.assertIsNone(self.code(V201, "Heartbeat", {}))
        self.assertIsNone(self.code(V16, "Heartbeat", {}))

    def test_keyword_mapping_2x(self):
        sn = {"timestamp": "2026-01-01T00:00:00Z", "connectorStatus": "Available", "evseId": 1, "connectorId": 1}
        self.assertEqual(self.code(V201, "StatusNotification", {**sn, "connectorStatus": "Exploded"}), "PropertyConstraintViolation")
        self.assertEqual(self.code(V201, "StatusNotification", {**sn, "evseId": "1"}), "TypeConstraintViolation")
        self.assertEqual(self.code(V201, "StatusNotification", {k: v for k, v in sn.items() if k != "evseId"}),
                         "OccurrenceConstraintViolation")
        self.assertEqual(self.code(V201, "Heartbeat", {"extra": 1}), "FormatViolation")

    def test_keyword_mapping_1_6(self):
        self.assertEqual(self.code(V16, "BootNotification", {"chargePointVendor": "x"}), "OccurenceConstraintViolation")
        self.assertEqual(self.code(V16, "StatusNotification", {"connectorId": 1, "errorCode": "NoError", "status": "Occupied"}),
                         "PropertyConstraintViolation")

    def test_file_naming(self):
        self.assertEqual(schemas.store(V16).schema_name("BootNotification", "request"), "BootNotification")
        self.assertEqual(schemas.store(V201).schema_name("BootNotification", "request"), "BootNotificationRequest")
        self.assertIn("StartTransaction", schemas.store(V16).request_actions)
        self.assertNotIn("StartTransaction", schemas.store(V201).request_actions)

    def test_send_actions_only_in_2_1(self):
        self.assertIn("NotifyPeriodicEventStream", schemas.store(V21).send_actions)
        self.assertEqual(schemas.store(V201).send_actions, set())
        self.assertEqual(schemas.store(V16).send_actions, set())


class Comparisons(unittest.TestCase):
    def test_core_provisioning_requests_unchanged_2_0_1_to_2_1(self):
        for action in ("BootNotification", "Heartbeat", "StatusNotification"):
            self.assertEqual(schemas.compare(action, "request", V201, V21)["status"], "same", action)

    def test_boot_response_only_longer_additional_info_in_2_1(self):
        c = schemas.compare("BootNotification", "response", V201, V21)
        changed = [(r["path"], r["a"]["maxLength"], r["b"]["maxLength"]) for r in c["rows"] if r["change"] != "same"]
        self.assertEqual(changed, [("statusInfo.additionalInfo", 512, 1024)])

    def test_boot_notification_reshaped_1_6_to_2_0_1(self):
        c = schemas.compare("BootNotification", "request", V16, V201)
        self.assertEqual(c["status"], "changed")
        self.assertIn("chargingStation", c["added"])
        self.assertIn("chargePointVendor", c["removed"])

    def test_replacements(self):
        c = schemas.compare("StartTransaction", "request", V16, V201)
        self.assertEqual(c["status"], "only 1.6")
        self.assertIn("TransactionEvent", c["replaced"])

    def test_transaction_event_changed_in_2_1(self):
        c = schemas.compare("TransactionEvent", "request", V201, V21)
        self.assertIn("costDetails", c["added"])

    def test_catalogue_covers_all_versions(self):
        cat = schemas.catalogue()
        boot = next(i for i in cat["items"] if i["action"] == "BootNotification" and i["kind"] == "request")
        self.assertEqual(boot["in"], {"1.6": True, "2.0.1": True, "2.1": True})


class Negotiation(unittest.TestCase):
    def test_server_preference_wins(self):
        self.assertIs(select(["ocpp2.0.1", "ocpp2.1"], ["2.1", "2.0.1", "1.6"]), V21)
        self.assertIs(select(["ocpp2.0.1", "ocpp2.1"], ["2.0.1"]), V201)
        self.assertIs(select(["ocpp1.6"], ["2.1", "2.0.1", "1.6"]), V16)

    def test_no_common_version(self):
        self.assertIsNone(select(["ocpp1.6"], ["2.1", "2.0.1"]))


if __name__ == "__main__":
    unittest.main()
