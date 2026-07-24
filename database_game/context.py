from __future__ import annotations

from datetime import date
from typing import Iterable

from database_game.models import Dimension, DimensionPacket, PublicContext


def build_public_context(
    *,
    project_id: int,
    project_name: str,
    source_inventory: dict[str, int],
    packets: Iterable[DimensionPacket],
    as_of_date: date | None = None,
) -> PublicContext:
    packet_map = {packet.dimension: packet for packet in packets}
    missing_packets = set(Dimension) - set(packet_map)
    if missing_packets:
        names = ", ".join(sorted(item.value for item in missing_packets))
        raise ValueError(f"missing dimension packets: {names}")
    global_missing = list(
        dict.fromkeys(
            item
            for dimension in Dimension
            for item in packet_map[dimension].missing_fields
        )
    )
    return PublicContext(
        project_id=project_id,
        project_name=project_name,
        as_of_date=as_of_date or date.today(),
        asset=packet_map[Dimension.ASSET],
        economic=packet_map[Dimension.ECONOMIC],
        legal=packet_map[Dimension.LEGAL],
        source_inventory=source_inventory,
        global_missing_fields=global_missing,
    )
