# Postmortem — DR Drill Lab 23

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-08-25T05:10:06 | Outage bắt đầu (Chaos kill Region A) | `chaos/chaos-events.jsonl:11` |
| 2026-08-25T05:10:06 | User đầu tiên bị ảnh hưởng (Lỗi 503 Timeout) | `reports/drill-2-withdr.jsonl:26` |
| 2026-08-25T05:10:26 | Health check alert Region A `UNHEALTHY` | `reports/health-events.jsonl:1` |
| 2026-08-25T05:10:29 | Operator confirm cutover & mở incident | `reports/runbook-run.jsonl:2` |
| 2026-08-25T05:10:29 | Restore snapshot hoàn tất sang Region B | `reports/failover-events.jsonl:2` |
| 2026-08-25T05:10:29 | Region B GPU pool warm-up sẵn sàng | `reports/failover-events.jsonl:4` |
| 2026-08-25T05:10:29 | DNS cutover trỏ active region về B | `reports/failover-events.jsonl:5` |
| 2026-08-25T05:10:36 | Resolved (Request đầu tiên thành công từ Region B) | `reports/drill-2-withdr.jsonl:40` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- **RTO**: Mục tiêu 300s · Đo được: `30.0s` · Gap: `-270.0s` (Vượt mức cam kết 270 giây).
- **RPO**: Mục tiêu 300s · Đo được: `10.0s` (`5` doc bị mất) · Gap: `-290.0s` (Dữ liệu mất mát thấp hơn nhiều so với SLA).
- **Bước tốn nhiều giây nhất:** `Health-check detection floor` (chiếm `20.1s` / `30.0s` $\approx 67\%$ tổng RTO).
  - *Vì sao:* Để chống hiện tượng flapping khi mạng chập chờn tạm thời, hệ thống bắt buộc phải chờ `threshold=3` lần fail liên tiếp với chu kỳ `interval=5s` và `timeout=2s`. Đây là khoảng trễ phát hiện cơ bản (Detection floor) bắt buộc phải trả giá để đảm bảo tính ổn định.

## 3. Root cause (5 whys)

1. *Tại sao người dùng nhận lỗi 503?* Vì Region A bị cô lập mạng (`netblock`), không thể phản hồi request.
2. *Tại sao hệ thống không phục vụ ngay từ Region B?* Vì Region B hoạt động ở mô hình Warm Standby, chưa được nạp snapshot vector DB mới nhất và GPU pool chưa scale lên `full`.
3. *Tại sao mất 20 giây để bắt đầu quy trình chuyển vùng?* Vì Health Checker cần xác nhận 3 lần probe thất bại liên tiếp trước khi nâng mức cảnh báo thành `UNHEALTHY`.
4. *Tại sao mất 5 documents (10.0s RPO)?* Vì cơ chế đồng bộ hóa snapshot giữa 2 region chạy định kỳ mỗi 30 giây (`every 30s`), các document nạp vào Region A trong 10 giây cuối chưa kịp replicate sang Region B trước khi sự cố xảy ra.
5. *Tại sao hệ thống không tự động rollback ngay khi Region A có tín hiệu sống lại?* Vì runbook quy định phải có gate phê duyệt thủ công và đồng bộ dữ liệu ngược (reverse replication) để ngăn ngừa thảm họa split-brain và flapping hai chiều.

## 4. Action items (có owner + deadline)

| # | Action Item | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Triển khai Change Data Capture (CDC) streaming cho Vector DB thay vì snapshot định kỳ 30s | Data Team | Q4/2026 | Giảm RPO từ 10s xuống < 1s (0 doc lost) |
| 2 | Giảm Edge Proxy TTL từ 5s xuống 1s và cấu hình Global Server Load Balancing (GSLB) Active Probing | Network SRE | Q4/2026 | Giảm RTO thêm ~4–5s ở bước DNS cache |
| 3 | Tự động hóa cảnh báo PagerDuty tích hợp webhook trigger Runbook 1-click execution | Platform Team | Q4/2026 | Giảm độ trễ phản hồi của operator xuống < 2s |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**
   - `interval × threshold = 5s × 3 = 15.0s`.
   - Thời gian phát hiện thực tế đo được là `20.1s`, chiếm **`67.0%`** tổng RTO đo được (`30.0s`).
2. **Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?**
   - Nếu hạ `interval` xuống 1s (với threshold=3), detection floor giảm từ 15s xuống 3s $\rightarrow$ RTO giảm được **12 giây**.
   - **Cái giá phải trả (Flapping risk):** Một sự cố mạng thoáng qua (network jitter / packet drop chốc lát trong 3 giây) sẽ bị kết luận nhầm là regional outage, kích hoạt toàn bộ quy trình failover tốn kém (restore, warm-up, DNS change), gây gián đoạn dịch vụ không đáng có và tạo ra hiện tượng flapping chuyển vùng qua lại liên tục giữa 2 region.
3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa gì với khách hàng?**
   - `docs_lost = 5 documents` (trong cửa sổ 10 giây cuối) là những dữ liệu đã được hệ thống xác nhận tiếp nhận từ khách hàng nhưng chưa kịp replicate sang Region B.
   - Khi Region A mất dữ liệu vĩnh viễn, 5 documents này sẽ bị mất hoàn toàn, dẫn đến tình trạng mất tính toàn vẹn dữ liệu (data loss). Đối với khách hàng, các tác vụ hoặc hóa đơn/thông tin liên quan đến 5 document này sẽ không thể truy vấn được trên hệ thống và cần cơ chế Write-Ahead Logging (WAL) bền vững hoặc hàng đợi tin nhắn đa vùng (Multi-region Message Queue như Kafka/PubSub) để replay lại.
