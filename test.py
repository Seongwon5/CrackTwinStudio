import trimesh

print("=" * 40)
print("🔍 Trimesh 가속 모듈 설치 및 활성화 여부 점검")
print("=" * 40)
print("1. rtree 설치 여부 (O/X):   ", trimesh.util.has_module('rtree'))
print("2. pyembree 설치 여부 (O/X):", trimesh.util.has_module('pyembree'))
print("=" * 40)
