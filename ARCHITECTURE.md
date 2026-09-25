# L3B Architecture Record

Team cập nhật tài liệu này cùng source để mô tả luồng thực thi đã được triển khai. Mục tiêu là báo cáo quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Hệ thống dùng mô hình orchestrator + specialist agents, tương tự cách ADC branch tách Business, SX, Warehousing thành các actor có trách nhiệm rõ ràng. Trong L3B, các specialist làm việc trên cùng `case_id` và chia sẻ evidence qua MCP, sau đó coordinator tổng hợp output và verifier kiểm tra schema & consistency.

```text
Input case -> Entity Resolver -> Coordinator -> Specialist agents -> Conflict resolver -> Verifier -> Output JSON
             |                           |                        |                   |             |
             └──────────── MCP evidence ───────────────────────┴─────────────────── Trace events
```

Luồng chính:
1. Nhận case và xác định entity scope (`order_id`, `customer_unique_id`, `seller_id`, `shipment_id`, `payment_reference`).
2. Khám phá tool list qua `gateway.list_tools()` và chọn subset theo domain: customer, order, shipment, payment, policy.
3. Gọi tool với `case_id` và tối thiểu 1-2 entity identifiers để thu evidence.
4. Coordinator phân công task cho order/customer, shipment, payment/refund, policy/conflict reviewer.
5. Hợp nhất evidence vào output theo schema `day09-l3b-output-v2`.
6. Verifier kiểm tra `case_id`, evidence refs, status consistency, payment totals, resolution action, confidence bounds, và trace lifecycle trước khi finalize.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | customer + order IDs | Resolve entity map, compare candidates, maintain customer context | customer history / order history | `entity_resolution`, `customer_context` |
| Coordinator | full case + discovered tools | Select tool set, assign work, manage handoff and final consolidation | discovery only | orchestrated plan and merged result |
| Order/product specialist | order/item data | Check order IDs, item set, claim scope, candidate ranking | order + item lookup | affected entities, candidate acceptance |
| Shipment specialist | shipment/timeline data | Determine late-delivery or logistics issue, complete timeline | shipment tool | `shipment_analysis` and related evidence |
| Payment/refund specialist | payment reference + refund data | Reconcile captured/refunded totals and risk of duplicate charges | payment/refund tool | `payment_analysis`, `financial_resolution` |
| Policy/conflict agent | policy and evidence conflicts | Resolve source precedence, explain conflict and policy implication | policy tool + source comparison | `data_conflicts`, `root_cause_analysis` |
| Verifier | final output + trace | Schema validation, evidence ownership, claim linkage, confidence sanity check | none | finalization or correction rerun |

Một nguyên tắc quan trọng là least privilege: không gọi tất cả tool cho mọi actor. Mỗi actor chỉ dùng tool tương ứng với domain và case evidence cần xét.

## 3. Entity resolution và A2A protocol

Entity resolution thực hiện theo tiến trình an toàn và hiệu quả:
- Duyệt các key khả dĩ như `order_id`, `order_ids`, `customer_unique_id`, `seller_id`, `shipment_id`, `payment_reference` trong case và nested object.
- Nếu có nhiều order IDs, ưu tiên danh sách trong case và chọn trường đầu tiên làm primary scope; phần còn lại giữ trong `resolved_order_ids` hoặc `rejected_candidates` tùy trường hợp.
- Tỷ lệ tin cậy (`confidence`) dựa trên số lượng evidence ref hợp lệ và mức độ rõ ràng của issue; nếu không rõ, giữ ở mức trung bình thay vì suy đoán.
- `case_id` là correlation key trong mọi event và evidence. Không dùng evidence chéo case; mỗi tool call đều truyền `case_id` đúng.
- Handoff chỉ diễn ra khi đã có 1 chứng cứ tối thiểu hoặc khi specialist xác định cần chuyển sang domain tiếp theo. Tránh vòng lặp bằng cách giới hạn `selected_tools[:6]` và không lặp lại tool đã dùng trên cùng `case_id`.

## 4. Evidence và conflict lifecycle

Mỗi MCP response được validate bởi `Contracts.validate_evidence(...)` trước khi dùng.
- `evidence_ref` bắt buộc phải có pattern `ev_...` và phải được giữ nguyên từ server.
- `data` phải là object JSON và domain phải nằm trong tập cho phép: order, item, payment, shipment, seller, customer, product, refund, policy.
- Mỗi `tool_result_consumed` event ghi lại `tool_name`, `evidence_refs` và `case_id` để kiểm tra provenance.
- Source conflict được mô hình hóa dưới `data_conflicts` với `field`, `sources`, `selected_source`, `resolution_code`.
- Khi dữ liệu không đủ, system không suy diễn giá trị giả; thực hiện `insufficient_evidence` và giữ `confidence` thấp hơn.
- Evidence không được tái sử dụng giữa các case; mỗi output đính kèm refs do chính call này sinh ra.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout | 1 retry, then fail closed | keep partial evidence only, mark insufficient | `tool_result_consumed` / `verification_completed` |
| Entity not found/ambiguous | 1 lookup pass | return `not_found` or `ambiguous` with low confidence | `task_assigned`, `handoff` |
| Source conflict | 0 extra retries | prefer source with strongest provenanced evidence | `policy_decided` |
| Invalid specialist result | 1 validation pass | drop invalid result and continue with partial evidence | `verification_completed` |

Chiến lược hiệu quả:
- Chỉ gọi tool có liên quan theo domain discovered.
- Không quét rộng hoặc gọi tool lặp lại trên cùng case.
- Cache tạm thời trong phạm vi `case_id`; không chia sẻ giữa case.
- Tối đa 6 tool call cho workflow chính, đủ để có evidence và không phá budget hiệu quả.

## 6. Verification invariants

Trước khi finalize, verifier kiểm tra các invariant sau:
- Schema phải hợp lệ với `l3b-output-v2.schema.json`.
- `case_id` phải khớp với input và `evidence_refs` phải thuộc case đang xử lý.
- `entity_resolution.resolved_order_ids` phải nằm trong affected entities.
- `payment_analysis.refunded_total_brl` và `financial_resolution.recommended_refund_brl` phải đồng nhất với logic của refund.
- `shipment_analysis` và `payment_analysis` phải tương thích với `primary_issue` và `responsible_parties`.
- `resolution_actions` phải là danh sách action hợp lý, không mâu thuẫn với `case_status`.
- `confidence` phải trong [0,1] và phản ánh mức độ evidence thực tế.

## 7. Reproducibility

- Runtime: Python 3.11+
- Package dependency: `httpx2`, `jsonschema[format]`, `mcp`, `python-dotenv`
- Cài đặt chuẩn: `python -m pip install -e '.[dev]'`
- Run: `day09 validate-inputs`, `day09 mcp-tools`, `day09 run`, `day09 validate`, `day09 package --output dist/submission.zip`
- Concurrency: workflow xử lý 1 case tại một thời điểm trong CLI `run`; không dùng xáo trộn random vì không cần stochastic model.
- Không lưu API key; chỉ giữ placeholder trong `.env.example`.
- Không chứa prompt bí mật hoặc chain-of-thought trong trace.
