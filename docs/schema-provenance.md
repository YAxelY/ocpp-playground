# Where the JSON schemas come from

By default the playground validates messages with the JSON schemas that ship inside the
[`ocpp`](https://pypi.org/project/ocpp/) Python library (`ocpp/v16`, `ocpp/v201`, `ocpp/v21`). These were compared
with the official files published by the Open Charge Alliance:

```bash
python -m playground.tools.verify_schemas 1.6   <OCPP_1.6_documentation>/schemas/json
python -m playground.tools.verify_schemas 2.0.1 <OCPP-2.0.1_part3_JSON_schemas>
python -m playground.tools.verify_schemas 2.1   <OCPP-2.1_part3_JSON_schemas>
```

Result (ocpp 2.1.0 vs the OCA downloads: 1.6 Edition 2, 2.0.1 Part 3 of 2020-03-31, 2.1 Edition 2):

| Version | Official | Library | Identical | Cosmetic differences only* | Structural differences |
|---|---|---|---|---|---|
| 1.6 | 56 | 78 | 0 | 55 | **1** · plus 22 extra files in the library |
| 2.0.1 | 128 | 128 | 0 | 127 | **1** |
| 2.1 | 181 | 181 | 181 | 0 | 0 |

\* descriptions, `$id`, `$schema`, `$comment`, `javaType`.

The structural differences:

| Schema | Official | Library |
|---|---|---|
| 1.6 `MeterValues`: `sampledValue.unit` enum | 17 units | 18: adds `Hertz` (the 1.6 errata say Frequency is reported in Hertz without adding it to the schema) |
| 2.0.1 `Get15118EVCertificateResponse.exiResponse` | `maxLength` 5600 | `maxLength` 7500 |

The 22 extra 1.6 files are 11 request/response pairs from the later **OCPP 1.6 security** white paper (an extension,
not part of the 1.6 Edition 2 schema set): CertificateSigned, DeleteCertificate, ExtendedTriggerMessage,
GetInstalledCertificateIds, GetLog, InstallCertificate, LogStatusNotification, SecurityEventNotification,
SignCertificate, SignedFirmwareStatusNotification, SignedUpdateFirmware. The schema explorer tags them 🔐.

## Using the official files instead

```bash
python -m playground --schemas 2.0.1=~/OCPP-2.0.1_part3_JSON_schemas --schemas 1.6=~/OCPP_1.6_documentation/schemas/json
```

The comparison tab then shows "official" as the source. The OCA documents are published under the Creative Commons
Attribution-NoDerivatives 4.0 licence and aren't included in this repository: download them from the
[Open Charge Alliance](https://openchargealliance.org/).
