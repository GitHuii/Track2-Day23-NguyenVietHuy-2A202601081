"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        **kw,
    }
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("FAILOVER", json.dumps(rec))
    return rec


def state_of(region: str) -> dict:
    """Lấy trạng thái từ /v1/state của region."""
    try:
        r = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {"region": region, "pool_state": "unknown", "weights": False, "count": 0}


def failover(target: str, backend: str, wait: float = 60.0) -> dict:
    """5 bước failover đúng thứ tự:
    1_verify_target    — /v1/state của target
    2_restore_snapshot — snapshot.get + snapshot.rpo()
    3_scale_pool       — ghi "full" vào state/region-<target>/pool_state
    4_wait_ready       — poll /readyz tới khi 200 (timeout -> abort)
    5_dns_cutover      — ghi target vào edge/active_region
    """
    primary = "a" if target == "b" else "b"

    # Bước 1: Verify target state
    st = state_of(target)
    emit(step="1_verify_target", target=target, state=st)

    # Bước 2: Restore snapshot & calculate RPO
    meta = snapshot.get(target, backend)
    prim_db = pathlib.Path(f"state/region-{primary}/vectors.sqlite")
    rest_db = pathlib.Path(f"state/region-{target}/vectors.sqlite")
    rpo_info = snapshot.rpo(prim_db, rest_db)
    rpo_s = rpo_info.get("rpo_seconds")
    docs_lost = rpo_info.get("docs_lost")
    embed_ver = meta.get("embed_model_version")

    emit(
        step="2_restore_snapshot",
        target=target,
        backend=backend,
        rpo_seconds=rpo_s,
        docs_lost=docs_lost,
        embed_model_version=embed_ver,
    )

    # Bước 3: Scale pool (warm -> full)
    pool_file = pathlib.Path(f"state/region-{target}/pool_state")
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    pool_file.write_text("full")
    emit(step="3_scale_pool", target=target, pool_state="full")

    # Bước 4: Wait ready (Poll /readyz)
    t_start = time.time()
    ready = False
    while time.time() - t_start < wait:
        try:
            r = httpx.get(f"{URL[target]}/readyz", timeout=1.5)
            if r.status_code == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.5)

    waited_s = round(time.time() - t_start, 2)
    if not ready:
        emit(
            step="4_wait_ready",
            target=target,
            ready=False,
            waited_s=waited_s,
            error="timeout_waiting_for_ready",
        )
        return {"ok": False, "target": target, "reason": "target_not_ready", "waited_s": waited_s}

    emit(step="4_wait_ready", target=target, ready=True, waited_s=waited_s)

    # Bước 5: DNS cutover (Chỉ khi target ready)
    active_region_file = pathlib.Path("edge/active_region")
    active_region_file.parent.mkdir(parents=True, exist_ok=True)
    active_region_file.write_text(target)
    emit(step="5_dns_cutover", active_region=target, target=target)

    final_state = state_of(target)
    return {
        "ok": True,
        "target": target,
        "rpo_seconds": rpo_s,
        "docs_lost": docs_lost,
        "embed_model_version": embed_ver,
        "waited_s": waited_s,
        "vector_count": final_state.get("count"),
        "has_weights": final_state.get("weights"),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
