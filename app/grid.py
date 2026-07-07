"""Uniform lat/lng grid over a bounding box.

Each cell is a square in degree-space with a stable id, row/col indices, and a
centre coordinate. Row/col make 8-neighbour lookups trivial for the routing
graph. (Production would use H3 hexes for equal-area cells; a simple grid keeps
the demo dependency-free and easy to reason about.)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Cell:
    id: str
    row: int
    col: int
    lat: float   # centre
    lng: float   # centre
    min_lat: float
    min_lng: float
    max_lat: float
    max_lng: float


@dataclass
class GridSpec:
    min_lat: float
    min_lng: float
    max_lat: float
    max_lng: float
    cell_deg: float

    @property
    def n_rows(self) -> int:
        return max(1, int(round((self.max_lat - self.min_lat) / self.cell_deg)))

    @property
    def n_cols(self) -> int:
        return max(1, int(round((self.max_lng - self.min_lng) / self.cell_deg)))


def cell_id(row: int, col: int) -> str:
    return f"r{row}_c{col}"


def build_cells(spec: GridSpec) -> list[Cell]:
    cells: list[Cell] = []
    for row in range(spec.n_rows):
        for col in range(spec.n_cols):
            min_lat = spec.min_lat + row * spec.cell_deg
            min_lng = spec.min_lng + col * spec.cell_deg
            max_lat = min_lat + spec.cell_deg
            max_lng = min_lng + spec.cell_deg
            cells.append(Cell(
                id=cell_id(row, col),
                row=row,
                col=col,
                lat=(min_lat + max_lat) / 2,
                lng=(min_lng + max_lng) / 2,
                min_lat=min_lat, min_lng=min_lng,
                max_lat=max_lat, max_lng=max_lng,
            ))
    return cells
