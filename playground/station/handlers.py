"""Station-side handlers: how the simulated station answers CSMS-initiated requests.

    StationHandlers2x   GetVariables, SetVariables, GetBaseReport, ChangeAvailability, Reset,
                        RequestStart/StopTransaction, TriggerMessage, UnlockConnector
    StationHandlers16   GetConfiguration, ChangeConfiguration, ChangeAvailability, Reset,
                        RemoteStart/StopTransaction, TriggerMessage, UnlockConnector, ClearCache

`@after` handlers run once the response has been sent (e.g. reboot after answering Reset).
"""
from __future__ import annotations

import asyncio

from ocpp.routing import after, on

from ..protocol.versions import OcppVersion
from .model import CONFIG_16


class StationCommon:
    R = None
    station = None

    def _availability(self, targets, operative: bool) -> str:
        busy = [e for e in targets if e.tx]
        for e in targets:
            if e.tx:
                e.pending_operative = operative          # applied when the transaction ends
            else:
                e.operative = operative
        asyncio.ensure_future(self.station.report([e for e in targets if not e.tx]))
        return "Scheduled" if busy else "Accepted"


class StationHandlers2x(StationCommon):
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
            results.append({"attribute_status": self.station.set_variable(key, item["attribute_value"]),
                            "component": item["component"], "variable": item["variable"]})
        return self.R.SetVariables(set_variable_result=results)

    @on("GetBaseReport")
    def on_get_base_report(self, request_id, report_base, **_):
        return self.R.GetBaseReport(status="Accepted")

    @after("GetBaseReport")
    async def after_get_base_report(self, request_id, report_base, **_):
        await self.station.send_report(self, request_id)

    @on("ChangeAvailability")
    def on_change_availability(self, operational_status, evse=None, **_):
        st = self.station
        targets = [st.evses[evse["id"]]] if evse and evse.get("id") in st.evses else list(st.evses.values())
        return self.R.ChangeAvailability(status=self._availability(targets, operational_status == "Operative"))

    @on("Reset")
    def on_reset(self, type, evse_id=None, **_):
        return self.R.Reset(status="Accepted")

    @after("Reset")
    async def after_reset(self, type, **_):
        await asyncio.sleep(0.3)
        await self.station.reboot("RemoteReset")

    @on("RequestStartTransaction")
    def on_request_start(self, id_token, remote_start_id, evse_id=None, **_):
        st = self.station
        e = st.evses.get(evse_id or 1)
        if st.registration != "Accepted":
            st.bus.note(st.id, "RequestStartTransaction while Pending → Rejected", ref="B02.FR.05")
            return self.R.RequestStartTransaction(status="Rejected")
        if e is None or not e.operative or e.fault or (e.tx and e.tx.token):
            return self.R.RequestStartTransaction(status="Rejected")
        return self.R.RequestStartTransaction(status="Accepted", **({"transaction_id": e.tx.id} if e.tx else {}))

    @after("RequestStartTransaction")
    async def after_request_start(self, id_token, remote_start_id, evse_id=None, **_):
        st = self.station
        e = st.evses.get(evse_id or 1)
        if st.registration == "Accepted" and e and e.operative and not e.fault and not (e.tx and e.tx.token):
            await st.behaviour.remote_start(e, id_token["id_token"], remote_start_id)

    @on("RequestStopTransaction")
    def on_request_stop(self, transaction_id, **_):
        return self.R.RequestStopTransaction(status="Accepted" if self.station.behaviour.find_tx(transaction_id) else "Rejected")

    @after("RequestStopTransaction")
    async def after_request_stop(self, transaction_id, **_):
        e = self.station.behaviour.find_tx(transaction_id)
        if e:
            await self.station.behaviour.end(e, "RemoteStop", "Remote")

    SUPPORTED_TRIGGERS = {"BootNotification", "Heartbeat", "StatusNotification"}

    @on("TriggerMessage")
    def on_trigger(self, requested_message, evse=None, **_):
        return self.R.TriggerMessage(status="Accepted" if requested_message in self.SUPPORTED_TRIGGERS else "NotImplemented")

    @after("TriggerMessage")
    async def after_trigger(self, requested_message, evse=None, **_):
        if requested_message in self.SUPPORTED_TRIGGERS:
            await self.station.triggered(requested_message, evse.get("id") if evse else None)

    @on("UnlockConnector")
    def on_unlock(self, evse_id, connector_id, **_):
        e = self.station.evses.get(evse_id)
        if e is None or connector_id != 1:
            return self.R.UnlockConnector(status="UnknownConnector")
        if e.tx and e.tx.token:                         # F05.FR.02: don't unlock during an authorized transaction
            return self.R.UnlockConnector(status="OngoingAuthorizedTransaction")
        return self.R.UnlockConnector(status="Unlocked")


class StationHandlers16(StationCommon):
    @on("GetConfiguration")
    def on_get_configuration(self, key=None, **_):
        known, unknown = [], []
        for k in key or list(CONFIG_16) + ["NumberOfConnectors"]:
            value = self.station.config_16(k)
            if value is None:
                unknown.append(k)
            else:
                known.append({"key": k, "readonly": value[1], "value": value[0]})
        return self.R.GetConfiguration(configuration_key=known, **({"unknown_key": unknown} if unknown else {}))

    @on("ChangeConfiguration")
    def on_change_configuration(self, key, value, **_):
        target = CONFIG_16.get(key)
        if target is None:
            return self.R.ChangeConfiguration(status="NotSupported")
        if isinstance(target, tuple):
            return self.R.ChangeConfiguration(status="Rejected")
        status = self.station.set_variable(target, value)
        return self.R.ChangeConfiguration(status="Accepted" if status == "Accepted" else "Rejected")

    @on("ChangeAvailability")
    def on_change_availability(self, connector_id, type, **_):
        st = self.station
        targets = list(st.evses.values()) if connector_id == 0 else [st.evses[connector_id]] if connector_id in st.evses else []
        if not targets:
            return self.R.ChangeAvailability(status="Rejected")
        return self.R.ChangeAvailability(status=self._availability(targets, type == "Operative"))

    @on("Reset")
    def on_reset(self, type, **_):
        return self.R.Reset(status="Accepted")

    @after("Reset")
    async def after_reset(self, type, **_):
        await asyncio.sleep(0.3)
        await self.station.reboot("RemoteReset", stop_reason="HardReset" if type == "Hard" else "SoftReset")

    @on("RemoteStartTransaction")
    def on_remote_start(self, id_tag, connector_id=None, **_):
        e = self.station.evses.get(connector_id or 1)
        ok = e is not None and e.operative and not e.fault and e.tx is None
        return self.R.RemoteStartTransaction(status="Accepted" if ok else "Rejected")

    @after("RemoteStartTransaction")
    async def after_remote_start(self, id_tag, connector_id=None, **_):
        e = self.station.evses.get(connector_id or 1)
        if e is not None and e.operative and not e.fault and e.tx is None:
            await self.station.behaviour.remote_start(e, id_tag, None)

    @on("RemoteStopTransaction")
    def on_remote_stop(self, transaction_id, **_):
        return self.R.RemoteStopTransaction(status="Accepted" if self.station.behaviour.find_tx(transaction_id) else "Rejected")

    @after("RemoteStopTransaction")
    async def after_remote_stop(self, transaction_id, **_):
        e = self.station.behaviour.find_tx(transaction_id)
        if e:
            await self.station.behaviour.end(e, "Remote")

    SUPPORTED_TRIGGERS = {"BootNotification", "Heartbeat", "StatusNotification", "MeterValues"}

    @on("TriggerMessage")
    def on_trigger(self, requested_message, connector_id=None, **_):
        return self.R.TriggerMessage(status="Accepted" if requested_message in self.SUPPORTED_TRIGGERS else "NotImplemented")

    @after("TriggerMessage")
    async def after_trigger(self, requested_message, connector_id=None, **_):
        if requested_message in self.SUPPORTED_TRIGGERS:
            await self.station.triggered(requested_message, connector_id)

    @on("UnlockConnector")
    def on_unlock(self, connector_id, **_):
        e = self.station.evses.get(connector_id)
        if e is None:
            return self.R.UnlockConnector(status="NotSupported")
        if e.tx:                                        # 1.6 §5.18: stop the transaction first
            asyncio.ensure_future(self.station.behaviour.end(e, "UnlockCommand"))
        return self.R.UnlockConnector(status="Unlocked")

    @on("ClearCache")
    def on_clear_cache(self, **_):
        return self.R.ClearCache(status="Accepted")


def station_class(version: OcppVersion):
    mixin = StationHandlers2x if version.is_2x else StationHandlers16
    return type(f"StationConnection_{version.name.replace('.', '_')}", (mixin, version.ChargePoint),
                {"R": version.call_result, "C": version.call})
