"""Station-side handlers: how the simulated charging station answers CSMS-initiated requests.

`self.R` is the call_result module of the negotiated version, `self.station` the Station.
`@after` handlers run once the response has been sent (e.g. reboot after answering Reset).
"""
from __future__ import annotations

import asyncio

from ocpp.routing import after, on

from ..protocol.versions import OcppVersion


class StationHandlers:
    R = None
    station = None

    # -- B05/B06 Set/GetVariables ------------------------------------------------------------------
    @on("GetVariables")
    def on_get_variables(self, get_variable_data, **_):
        results = []
        for item in get_variable_data:
            key = f"{item['component']['name']}.{item['variable']['name']}"
            entry = self.station.variables.get(key)
            known_component = any(k.startswith(item["component"]["name"] + ".") for k in self.station.variables)
            status = "Accepted" if entry else ("UnknownVariable" if known_component else "UnknownComponent")
            result = {"attribute_status": status, "component": item["component"], "variable": item["variable"]}
            if entry:
                result["attribute_value"] = str(entry[0])
            results.append(result)
        return self.R.GetVariables(get_variable_result=results)

    @on("SetVariables")
    def on_set_variables(self, set_variable_data, **_):
        results = []
        for item in set_variable_data:
            key = f"{item['component']['name']}.{item['variable']['name']}"
            entry = self.station.variables.get(key)
            if entry is None:
                status = "UnknownVariable"
            elif entry[1] != "ReadWrite":
                status = "Rejected"
            else:
                try:
                    entry[0] = int(item["attribute_value"]) if isinstance(entry[0], int) else item["attribute_value"]
                    status = "Accepted"
                except ValueError:
                    status = "Rejected"
            results.append({"attribute_status": status, "component": item["component"], "variable": item["variable"]})
        self.station.bus.changed()
        return self.R.SetVariables(set_variable_result=results)

    # -- B07 GetBaseReport → NotifyReport ----------------------------------------------------------
    @on("GetBaseReport")
    def on_get_base_report(self, request_id, report_base, **_):
        return self.R.GetBaseReport(status="Accepted")

    @after("GetBaseReport")
    async def after_get_base_report(self, request_id, report_base, **_):
        await self.station.send_report(self, request_id)

    # -- G03/G04 ChangeAvailability ----------------------------------------------------------------
    @on("ChangeAvailability")
    def on_change_availability(self, operational_status, evse=None, **_):
        targets = [self.station.evses[evse["id"]]] if evse and evse.get("id") in self.station.evses \
            else list(self.station.evses.values())
        operative = operational_status == "Operative"
        busy = [e for e in targets if e.tx]
        for e in targets:
            if e.tx:
                e.pending_operative = operative
            else:
                e.operative = operative
        asyncio.ensure_future(self.station.report_status(self, [e for e in targets if not e.tx]))
        return self.R.ChangeAvailability(status="Scheduled" if busy else "Accepted")

    # -- B11/B12 Reset ---------------------------------------------------------------------------
    @on("Reset")
    def on_reset(self, type, evse_id=None, **_):
        return self.R.Reset(status="Accepted")

    @after("Reset")
    async def after_reset(self, type, **_):
        await asyncio.sleep(0.3)
        await self.station.reboot(self, "RemoteReset")

    # -- F01/F02 RequestStartTransaction -----------------------------------------------------------
    @on("RequestStartTransaction")
    def on_request_start(self, id_token, remote_start_id, evse_id=None, **_):
        st = self.station
        evse = st.evses.get(evse_id or 1)
        if st.registration != "Accepted":
            st.bus.note(st.id, "RequestStartTransaction while Pending → Rejected", ref="B02.FR.05")
            return self.R.RequestStartTransaction(status="Rejected")
        if evse is None or not evse.operative or evse.fault or (evse.tx and evse.tx.token):
            return self.R.RequestStartTransaction(status="Rejected")
        extra = {"transaction_id": evse.tx.id} if evse.tx else {}
        return self.R.RequestStartTransaction(status="Accepted", **extra)

    @after("RequestStartTransaction")
    async def after_request_start(self, id_token, remote_start_id, evse_id=None, **_):
        st = self.station
        evse = st.evses.get(evse_id or 1)
        if st.registration == "Accepted" and evse and evse.operative and not evse.fault and not (evse.tx and evse.tx.token):
            await st.remote_start(self, evse, id_token["id_token"], remote_start_id)

    # -- F03 RequestStopTransaction ----------------------------------------------------------------
    @on("RequestStopTransaction")
    def on_request_stop(self, transaction_id, **_):
        found = any(e.tx and e.tx.id == transaction_id for e in self.station.evses.values())
        return self.R.RequestStopTransaction(status="Accepted" if found else "Rejected")

    @after("RequestStopTransaction")
    async def after_request_stop(self, transaction_id, **_):
        for e in self.station.evses.values():
            if e.tx and e.tx.id == transaction_id:
                await self.station.end_transaction(self, e, "RemoteStop", "Remote")

    # -- F06 TriggerMessage ------------------------------------------------------------------------
    SUPPORTED_TRIGGERS = {"BootNotification", "Heartbeat", "StatusNotification"}

    @on("TriggerMessage")
    def on_trigger(self, requested_message, evse=None, **_):
        ok = requested_message in self.SUPPORTED_TRIGGERS
        return self.R.TriggerMessage(status="Accepted" if ok else "NotImplemented")

    @after("TriggerMessage")
    async def after_trigger(self, requested_message, evse=None, **_):
        if requested_message in self.SUPPORTED_TRIGGERS:
            await self.station.triggered(self, requested_message, evse.get("id") if evse else None)


def station_class(version: OcppVersion):
    return type(f"StationConnection_{version.name.replace('.', '_')}", (StationHandlers, version.ChargePoint),
                {"R": version.call_result, "C": version.call})
