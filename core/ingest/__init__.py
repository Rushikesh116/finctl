"""Adapters that map a real gateway's wire format onto FinCtl's canonical schema.

Nothing here reads ground truth, and nothing here is on the reconciliation path: an adapter
produces `core.records` objects and stops. The cascade cannot tell whether a `GatewayRow` came
from a generated dataset or from a real settlement, which is the point — if it could, the
adapter would be a second matcher.
"""
