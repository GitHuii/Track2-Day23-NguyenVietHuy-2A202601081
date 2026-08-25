"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


from dr import health_checker as hc  # noqa: E402


def step(n: int, name: str, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG và in ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "step": n,
        "name": name,
        **kw,
    }
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("RUNBOOK", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    try:
        ans = input(f"{msg} [y/N]: ").strip().lower()
        return ans in ("y", "yes")
    except EOFError:
        return False


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước quy trình Runbook:
    1 xac_nhan_outage          — probe cả 2 region
    2 thong_bao_incident       — ghi nhận incident clock, tính delay thông báo
    3 scale_gpu_pool           — gọi failover.failover(...) DUY NHẤT 1 LẦN
    4 verify_state_replica     — đọc kết quả state từ bước 3
    5 dns_cutover              — đọc kết quả cutover từ bước 3
    6 verify_golden_signals    — 10 request kiểm tra p95 latency + error rate
    7 post_incident            — tổng kết thời gian và lệnh đo RTO
    """
    t_start = time.time()

    # Bước 1: Xác nhận outage
    ready_prim, reason_prim = hc.probe(primary, timeout=1.5)
    ready_tgt, reason_tgt = hc.probe(target, timeout=1.5)
    step(
        1,
        "xac_nhan_outage",
        primary=primary,
        primary_ready=ready_prim,
        primary_reason=reason_prim,
        target=target,
        target_ready=ready_tgt,
        target_reason=reason_tgt,
    )

    # Bước 2: Thông báo incident
    chaos_file = pathlib.Path("chaos/chaos-events.jsonl")
    t_outage = None
    if chaos_file.exists():
        kills = [
            json.loads(line)
            for line in chaos_file.read_text().splitlines()
            if line.strip() and '"action": "kill"' in line
        ]
        if kills:
            t_outage = kills[-1].get("ts")

    ts_notify = time.time()
    delay_s = round(ts_notify - t_outage, 2) if t_outage else None
    step(
        2,
        "thong_bao_incident",
        t_outage=t_outage,
        ts_notify=ts_notify,
        notification_delay_s=delay_s,
        note="Operator confirmed outage and opened incident clock",
    )

    if not confirm(auto, f"Xac nhan failover tu {primary} sang {target}?"):
        print("Failover bi huy bo boi nguoi van hanh.")
        return {"ok": False, "aborted": True}

    # Bước 3: Scale GPU pool & Failover (Chỉ gọi 1 lần duy nhất)
    fo_res = fo.failover(target=target, backend=backend)
    step(
        3,
        "scale_gpu_pool",
        failover_ok=fo_res.get("ok"),
        target=target,
        waited_s=fo_res.get("waited_s"),
    )
    if not fo_res.get("ok"):
        return {"ok": False, "error": "failover_failed", "detail": fo_res}

    # Bước 4: Verify state replica
    step(
        4,
        "verify_state_replica",
        target=target,
        vector_count=fo_res.get("vector_count"),
        has_weights=fo_res.get("has_weights"),
        rpo_seconds=fo_res.get("rpo_seconds"),
        docs_lost=fo_res.get("docs_lost"),
        embed_model_version=fo_res.get("embed_model_version"),
    )

    # Bước 5: DNS cutover
    step(5, "dns_cutover", active_region=target, cutover_ok=fo_res.get("ok"))

    # Bước 6: Verify golden signals (10 requests thật)
    latencies = []
    errors = 0
    for _ in range(10):
        t_req = time.time()
        try:
            r = httpx.get(f"{URL[target]}/v1/infer", timeout=2.0)
            if r.status_code == 200:
                latencies.append((time.time() - t_req) * 1000.0)
            else:
                errors += 1
        except Exception:
            errors += 1
        time.sleep(0.05)

    latencies.sort()
    p95 = round(latencies[int(len(latencies) * 0.95)] if latencies else 0.0, 1)
    err_rate = round(errors / 10.0, 2)
    step(
        6,
        "verify_golden_signals",
        target=target,
        total_requests=10,
        p95_latency_ms=p95,
        error_rate=err_rate,
    )

    # Bước 7: Post incident summary
    elapsed = round(time.time() - t_start, 2)
    measure_cmd = "python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300"
    step(7, "post_incident", elapsed_s=elapsed, measure_command=measure_cmd)

    return {
        "ok": True,
        "elapsed_s": elapsed,
        "target": target,
        "failover": fo_res,
        "golden_signals": {"p95_latency_ms": p95, "error_rate": err_rate},
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
