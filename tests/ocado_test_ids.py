"""Deterministic, synthetic UUIDs for mocked Ocado products."""

from uuid import NAMESPACE_URL, uuid5


def product_id(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, 'test-ocado-product:' + label))
