# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-08-25T04:33:03` | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+0.3s` | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:18` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0s | `action:kill` | `chaos/chaos-events.jsonl:11` |
| User thấy lỗi đầu tiên | +0.2s | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:26` |
| Health check phát hiện | +20.1s | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:1` |
| Snapshot restore xong | +23.6s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | +23.7s | `step:4_wait_ready` | `reports/failover-events.jsonl:4` |
| DNS cutover | +23.7s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | +30.0s | dòng `ok:true` đầu sau lỗi | `reports/drill-2-withdr.jsonl:40` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `30.0s` | 300s (5 phút) | PASS |
| RPO — Vector DB | `10.0s` / `5` doc | 300s (5 phút) | PASS |

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 15.0s | `interval_s × threshold` trong `reports/health-events.jsonl:1` | Giảm interval (ví dụ từ 5s xuống 2s) hoặc threshold (từ 3 xuống 2), nhưng đánh đổi tăng nguy cơ flapping khi mạng chập chờn. |
| Snapshot restore | 0.1s | 2_restore → 3_scale trong `reports/failover-events.jsonl:2` | Dùng Continuous CDC replication hoặc incremental snapshot thay vì full DB snapshot. |
| GPU pool warm-up | 0.14s | `waited_s` ở `4_wait_ready` trong `reports/failover-events.jsonl:4` | Duy trì Region B ở trạng thái Warm/Hot standby với weights đã nạp sẵn vào VRAM (Active-Active hoặc Warm Pool). |
| DNS/LB TTL cache | 6.3s | t_recovered − t_cutover trong `reports/drill-2-withdr.jsonl:40` | Hạ DNS TTL / Edge Proxy TTL (ví dụ từ 5s xuống 1s) hoặc dùng Anycast BGP routing / Global Server Load Balancing với active probing. |
