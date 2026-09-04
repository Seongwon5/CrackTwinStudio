# -*- coding: utf-8 -*-
"""twin_mesh.py — .twin 에서 메시 정점 표본을 꺼낸다(mesh.ply 원본 · 계층 LOD 타일 둘 다).

compare_twins(정합)와 export_report(위치도)는 정점 '표본'만 필요하다. 타일 보고서(tiler.py)에는
mesh.ply 가 없지만, 루트·상위 타일이 곧 메시를 요약한 정점 집합이라 그대로 표본이 된다.
타일 좌표는 국소좌표(원점을 뺀 값)이므로 meta.tiles.origin 을 다시 더해 절대좌표로 돌려준다 —
meta 의 리본·핫스팟 좌표와 같은 계에 놓이게.
"""
import io
import json
import struct

import numpy as np

_MAGIC = b"TWT1"


def _tile_positions(buf):
    """tiles/<id>.bin → (nV,3) float32 국소좌표(형식은 tiler.py 머리말)."""
    if buf[:4] != _MAGIC:
        raise ValueError("타일 형식 오류")
    nV, nF, flags = struct.unpack("<III", buf[4:16])
    return np.frombuffer(buf, dtype="<f4", count=nV * 3, offset=16).reshape(nV, 3)


def vertex_sample(z, n, seed=0, meta=None):
    """ZipFile z 에서 정점 표본 최대 n개(절대좌표, float64 (k,3))를 돌려준다.

    mesh.ply 가 있으면 trimesh 로 전체를 읽어 표본을 뽑고(예전 경로), 없으면 타일을 루트부터
    너비 우선으로 읽어 정점이 n 의 3배를 넘을 때까지 모은 뒤 표본을 뽑는다.
    """
    names = set(z.namelist())
    rng = np.random.default_rng(seed)
    if "mesh.ply" in names:
        import trimesh
        mesh = trimesh.load(io.BytesIO(z.read("mesh.ply")), file_type="ply", process=False)
        V = np.asarray(mesh.vertices, float)
    else:
        meta = meta or json.loads(z.read("meta.json"))
        t = meta.get("tiles")
        if not t:
            raise ValueError("보고서에 mesh.ply 도 타일도 없습니다")
        nodes, origin = t["nodes"], np.asarray(t["origin"], float)
        parts, total, queue = [], 0, [t["root"]]
        while queue and total < n * 3:
            nid = queue.pop(0)
            P = _tile_positions(z.read(f"tiles/{nid}.bin")).astype(np.float64) + origin
            parts.append(P); total += len(P)
            queue.extend(nodes[nid]["c"])
        V = np.concatenate(parts)
    if len(V) > n:
        V = V[rng.choice(len(V), n, replace=False)]
    return V
