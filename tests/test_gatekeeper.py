"""Unit tests for the gatekeeper: the protocol rules applied before the ocpp library."""
import json
import unittest

from playground.csms import gatekeeper
from playground.csms.state import Policy, StationRecord
from playground.protocol import V21, V201

HANDLED = {"BootNotification", "Heartbeat", "StatusNotification", "Authorize", "TransactionEvent"}
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
        for registration in ("Unknown", "Pending", "Rejected"):
            self.rec.registration = registration
            r = reply(self.inspect(f'[2,"h-{registration}","Heartbeat",{{}}]'))
            self.assertEqual(r[2], "SecurityError", registration)

    def test_triggered_message_allowed_once_while_pending(self):
        self.rec.registration = "Pending"
        self.rec.allowed_once.add("Heartbeat")
        self.assertIsNotNone(self.inspect('[2,"h1","Heartbeat",{}]').forward)
        self.assertEqual(reply(self.inspect('[2,"h2","Heartbeat",{}]'))[2], "SecurityError")

    def test_unknown_action_not_implemented(self):
        self.rec.registration = "Accepted"
        self.assertEqual(reply(self.inspect('[2,"x","MakeCoffee",{}]'))[2], "NotImplemented")

    def test_known_but_unhandled_action_not_supported(self):
        self.rec.registration = "Accepted"
        r = reply(self.inspect('[2,"x","SecurityEventNotification",{"type":"t","timestamp":"2026-01-01T00:00:00Z"}]'))
        self.assertEqual(r[2], "NotSupported")

    def test_unreadable_id_answer_or_drop(self):
        self.assertEqual(reply(self.inspect('[2, "x", "Heartbeat", {'))[1], "-1")
        self.policy.unreadable_frames = "drop"
        v = self.inspect('[2, "x", "Heartbeat", {')
        self.assertIsNone(v.reply)
        self.assertIsNone(v.forward)

    def test_unknown_type_answered_in_2_0_1_ignored_in_2_1(self):
        self.assertEqual(reply(self.inspect('[7,"t","Heartbeat",{}]', V201))[2], "MessageTypeNotSupported")
        v = self.inspect('[7,"t2","Heartbeat",{}]', V21)
        self.assertIsNone(v.reply)
        self.assertIsNone(v.forward)

    def test_send_frame_is_unknown_in_2_0_1(self):
        self.assertEqual(reply(self.inspect('[6,"s","NotifyPeriodicEventStream",{}]', V201))[2], "MessageTypeNotSupported")

    def test_send_frame_never_answered_in_2_1(self):
        valid = '[6,"s1","NotifyPeriodicEventStream",{"id":1,"pending":0,"basetime":"2026-01-01T00:00:00Z","data":[{"t":0,"v":"230"}]}]'
        invalid = '[6,"s2","NotifyPeriodicEventStream",{"id":1}]'
        v1, v2 = self.inspect(valid, V21), self.inspect(invalid, V21)
        self.assertIsNotNone(v1.send)
        for v in (v1, v2):
            self.assertIsNone(v.reply)
            self.assertIsNone(v.forward)

    def test_schema_violation_code(self):
        self.rec.registration = "Accepted"
        r = reply(self.inspect('[2,"s","StatusNotification",{"timestamp":"2026-01-01T00:00:00Z",'
                               '"connectorStatus":"Exploded","evseId":1,"connectorId":1}]'))
        self.assertEqual(r[2], "PropertyConstraintViolation")
        self.assertEqual(r[4]["path"], "connectorStatus")

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
