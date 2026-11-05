"""Unit tests for the gatekeeper: the protocol rules applied before the ocpp library, per version."""
import json
import unittest

from playground.csms import gatekeeper
from playground.csms.state import Policy, StationRecord
from playground.protocol import V16, V21, V201

HANDLED = {"BootNotification", "Heartbeat", "StatusNotification", "Authorize", "TransactionEvent",
           "StartTransaction", "StopTransaction", "MeterValues"}
BOOT = '[2,"b1","BootNotification",{"reason":"PowerUp","chargingStation":{"model":"m","vendorName":"v"}}]'


def reply(verdict):
    return json.loads(verdict.reply.to_json()) if verdict.reply else None


class Gatekeeper(unittest.TestCase):
    def setUp(self):
        self.rec = StationRecord(id="CS")
        self.policy = Policy()

    def inspect(self, raw, version=V201):
        return gatekeeper.inspect(raw, version, self.rec, self.policy, HANDLED)

    def test_boot_is_always_forwarded(self):
        self.assertEqual(self.inspect(BOOT).forward, BOOT)

    def test_security_error_before_accepted(self):
        for version in (V201, V21, V16):
            for registration in ("Unknown", "Pending", "Rejected"):
                self.rec.registration = registration
                r = reply(self.inspect(f'[2,"h-{version.name}-{registration}","Heartbeat",{{}}]', version))
                self.assertEqual(r[2], "SecurityError", (version.name, registration))

    def test_triggered_message_allowed_once_while_pending(self):
        self.rec.registration = "Pending"
        self.rec.allowed_once.add("Heartbeat")
        self.assertIsNotNone(self.inspect('[2,"h1","Heartbeat",{}]').forward)
        self.assertEqual(reply(self.inspect('[2,"h2","Heartbeat",{}]'))[2], "SecurityError")

    def test_unknown_action_not_implemented(self):
        self.rec.registration = "Accepted"
        for version in (V201, V16):
            self.assertEqual(reply(self.inspect(f'[2,"x{version.name}","MakeCoffee",{{}}]', version))[2], "NotImplemented")

    def test_version_specific_actions(self):
        self.rec.registration = "Accepted"
        tx16 = '[2,"s1","StartTransaction",{"connectorId":1,"idTag":"A","meterStart":0,"timestamp":"2026-01-01T00:00:00Z"}]'
        self.assertIsNotNone(self.inspect(tx16, V16).forward)
        self.assertEqual(reply(self.inspect(tx16.replace("s1", "s2"), V201))[2], "NotImplemented")   # no such message in 2.x

    def test_known_but_unhandled_action_not_supported(self):
        self.rec.registration = "Accepted"
        r = reply(self.inspect('[2,"x","SecurityEventNotification",{"type":"t","timestamp":"2026-01-01T00:00:00Z"}]'))
        self.assertEqual(r[2], "NotSupported")

    def test_unreadable_id_answer_or_drop(self):
        for version, code in ((V201, "RpcFrameworkError"), (V16, "GenericError")):
            r = reply(self.inspect('[2, "x", "Heartbeat", {', version))
            self.assertEqual((r[1], r[2]), ("-1", code))
        self.policy.unreadable_frames = "drop"
        v = self.inspect('[2, "x", "Heartbeat", {')
        self.assertIsNone(v.reply)
        self.assertIsNone(v.forward)

    def test_unknown_type_answered_only_in_2_0_1(self):
        self.assertEqual(reply(self.inspect('[7,"t","Heartbeat",{}]', V201))[2], "MessageTypeNotSupported")
        for version in (V21, V16):
            v = self.inspect('[7,"t2","Heartbeat",{}]', version)
            self.assertIsNone(v.reply)
            self.assertIsNone(v.forward)

    def test_send_frame_unknown_before_2_1(self):
        self.assertEqual(reply(self.inspect('[6,"s","NotifyPeriodicEventStream",{}]', V201))[2], "MessageTypeNotSupported")
        self.assertIsNone(self.inspect('[6,"s2","NotifyPeriodicEventStream",{}]', V16).reply)   # 1.6: ignored

    def test_send_frame_never_answered_in_2_1(self):
        valid = '[6,"s1","NotifyPeriodicEventStream",{"id":1,"pending":0,"basetime":"2026-01-01T00:00:00Z","data":[{"t":0,"v":"230"}]}]'
        invalid = '[6,"s2","NotifyPeriodicEventStream",{"id":1}]'
        v1, v2 = self.inspect(valid, V21), self.inspect(invalid, V21)
        self.assertIsNotNone(v1.send)
        for v in (v1, v2):
            self.assertIsNone(v.reply)
            self.assertIsNone(v.forward)

    def test_schema_violation_codes_per_version(self):
        self.rec.registration = "Accepted"
        r = reply(self.inspect('[2,"s","StatusNotification",{"timestamp":"2026-01-01T00:00:00Z",'
                               '"connectorStatus":"Exploded","evseId":1,"connectorId":1}]'))
        self.assertEqual((r[2], r[4]["path"]), ("PropertyConstraintViolation", "connectorStatus"))
        r = reply(self.inspect('[2,"s16","Heartbeat",{"extra":1}]', V16))
        self.assertEqual(r[2], "FormationViolation")

    def test_duplicate_message_id(self):
        self.rec.registration = "Accepted"
        self.assertIsNotNone(self.inspect('[2,"same","Heartbeat",{}]').forward)
        self.assertEqual(reply(self.inspect('[2,"same","Heartbeat",{}]'))[2], "RpcFrameworkError")
        self.policy.duplicate_ids = "allow"
        self.assertIsNotNone(self.inspect('[2,"same","Heartbeat",{}]').forward)

    def test_answers_are_forwarded_to_the_library(self):
        for raw in ('[3,"r",{}]', '[4,"r","GenericError","",{}]'):
            self.assertEqual(self.inspect(raw).forward, raw)

    def test_callresulterror_is_logged_not_forwarded(self):
        v = self.inspect('[5,"r","FormatViolation","bad",{}]', V21)
        self.assertIsNone(v.forward)
        self.assertTrue(v.notes)


if __name__ == "__main__":
    unittest.main()
