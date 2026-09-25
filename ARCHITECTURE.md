# L3B Architecture Record

Tài liệu này mô tả các quyết định có thể kiểm chứng của workflow. Trace chỉ chứa sự kiện quan sát được, không chứa prompt, bí mật hay chain-of-thought.

## 1. Luồng hệ thống

```text
Case input
   │
   ▼
Coordinator ──handoff──► Entity Agent ──MCP: order/customer──┐
   │                                                        │
   ├──handoff──► Order/Item Agent ──MCP: items/product/seller├──► Policy Agent
   ├──handoff──► Payment Agent ──MCP: payment/refund─────────┤       │
   └──handoff──► Shipment Agent ──MCP: shipment──────────────┘       ▼
                                                               Verifier
                                                                  │
                                                                  ▼
                                                         L3B output + trace
```

Coordinator khám phá tool trước mỗi case, resolve phạm vi order, giao việc theo domain và chỉ bật các truy vấn sâu phù hợp với nội dung claim. Kết quả nội bộ của specialist không phải public contract; chỉ output cuối, MCP envelope, trace event và submission manifest bị khóa bởi JSON Schema trong `contracts/schemas/`.

## 2. Phân quyền agent

| Actor | Đầu vào | Trách nhiệm | Tool được phép | Handoff |
| --- | --- | --- | --- | --- |
| Coordinator | Case gốc, danh mục tool | Điều phối, giới hạn scope/budget, tổng hợp | Chỉ discovery; không gọi tool dữ liệu | Task đến specialist; bản nháp đến verifier |
| Entity Agent | Exact/candidate order IDs | Xếp hạng candidate, tìm customer và related orders | `get_order`, `get_customer_history` | `resolved`, `ambiguous` hoặc `not_found` |
| Order/Item Agent | Resolved order IDs | Item, product và seller context | `get_order_items`, `get_product_context`, `get_sellers` | Entity sets và seller responsibility |
| Payment Agent | Resolved order IDs, claim cues | Capture, mismatch, refund và số tiền | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | Payment verdict và refundable total |
| Shipment Agent | Resolved order IDs, claim cues | Timeline giao hàng, seller/logistics delay | `get_shipment_summary` | Shipment verdict và late sellers |
| Policy Agent | Policy version và evidence đã thu | Áp dụng policy/source precedence | `get_policy` | Issue, status, actions và refund decision |
| Verifier Agent | Bản nháp tổng hợp | Kiểm tra invariant/public contract | Không có quyền MCP | Validated output hoặc lỗi cứng |

Quyền được thực thi bằng allow-list `TOOL_OWNER`. Tool discovery không cấp thêm quyền cho agent; tool vừa phải tồn tại ở gateway vừa phải thuộc allow-list của vai trò.

## 3. A2A và entity resolution

Thông điệp handoff được biểu diễn qua trace với `case_id`, `actor`, `target`, `decision_code`, `evidence_refs` và các thuộc tính đếm không nhạy cảm. `case_id` là correlation ID xuyên suốt. Không chuyển dữ liệu hoặc evidence giữa hai case.

Entity Agent ưu tiên exact `order_id`; nếu chỉ có candidate thì gọi `get_order` tối đa năm candidate, loại record không tồn tại và xếp hạng theo ID/hint trùng khớp (customer, seller, product, payment, shipment). Một top score duy nhất được resolve; đồng hạng là `ambiguous`; không có record hợp lệ là `not_found`. Chỉ tối đa hai exact order được điều tra sâu để chặn fan-out. Handoff chỉ đi theo DAG phía trên, vì vậy không có vòng lặp A2A.

## 4. Vòng đời evidence và conflict

1. `EvidenceGateway` validate mọi MCP response bằng `mcp-evidence-response-v1.schema.json` trước khi trả cho agent.
2. Cache trong bộ nhớ dùng khóa `(tool_name, sorted arguments)` và chỉ sống trong một lần `solve_case`.
3. Khi evidence được dùng, workflow emit `tool_result_consumed` với đúng `tool_name` và `evidence_ref` do server cấp.
4. Output chỉ tham chiếu evidence đã nhận trong chính case đó. Không sửa, băm lại hoặc tự tạo `evidence_ref`.
5. Conflict trên các trạng thái trọng yếu được giữ trong `data_conflicts`. Source sự kiện chuyên ngành được ưu tiên theo thứ tự refund, payment, shipment, order; conflict chưa đủ căn cứ hạ confidence và giữ case ở trạng thái cần điều tra khi thích hợp.

## 5. Retry, failure và hiệu quả

| Tình huống | Retry budget | Fallback | Trace/code |
| --- | ---: | --- | --- |
| MCP timeout/lỗi gọi hoặc envelope sai | Tối đa 3 attempts/tool+args, backoff 0.75s/1.5s | Ghi failure, không bịa dữ liệu, hạ confidence | `handoff` / `MCP_CALL_FAILED` |
| Entity không tìm thấy | Không quét ngoài candidate input | `not_found`, `needs_investigation` | `ENTITY_NOT_FOUND` |
| Candidate đồng hạng | Không tự chọn ngẫu nhiên | `ambiguous`, không điều tra sâu | `ENTITY_AMBIGUOUS` |
| Source conflict | Không retry chỉ để đổi kết quả | Ghi `data_conflicts`, áp precedence | `policy_decided` |
| Thiếu optional tool | 0 | Bỏ domain call, dùng `insufficient_evidence` | Thể hiện ở verdict/confidence |

Các call nền gồm order, items và payments. Timeline shipment/refund/payment, seller và product chỉ được gọi khi claim có cue tương ứng. Cache ngăn gọi trùng; candidate/order và evidence refs đều có hard cap theo schema.

## 6. Invariant trước finalize

Verifier kiểm tra các điều kiện sau trước khi CLI ghi file:

- chỉ dùng field được schema L3B cho phép; version và `case_id` phải khớp;
- `affected_entities.order_ids` bằng resolved scope và không chứa rejected candidate;
- mỗi evidence ref bắt nguồn từ MCP call của case hiện tại và xuất hiện trong trace;
- verdict shipment/payment dùng enum contract; timeline thiếu không được suy đoán thành on-time;
- mọi amount hữu hạn, không âm, làm tròn hai chữ số; refund lines cộng nhất quán với refund đề xuất;
- seller responsibility chỉ đi cùng seller evidence; logistics/payment responsibility khớp issue;
- `no_action` không tạo refund/action tài chính; evidence thiếu dẫn đến `needs_investigation`;
- confidence nằm trong `[0,1]` và bị hạ khi ambiguous, conflict hoặc MCP failure;
- output được `Contracts.validate_output` kiểm tra lại tại biên ghi file.

### Policy và hiệu chuẩn confidence

`contracts/scoring/scoring-policy-v2.json` quy định hard gates và trọng số đánh giá; nó không được diễn giải như bảng luật hoàn tiền. Luật trọng tài nghiệp vụ đến từ evidence `get_policy` theo `policy_version`. Policy Agent áp dụng eligibility, tỷ lệ và refund cap khi các field này có trong rule của đúng `primary_issue`; nếu policy evidence thiếu thì không tự dựng rule mới.

Số tiền thanh toán dùng aggregate có thẩm quyền nếu có, nếu không mới cộng các payment lines. Với `payment_mismatch` hoặc `duplicate_charge`, refund chỉ là phần `captured - payable - already_refunded`; với order đã hủy/unavailable hoặc refund thất bại, refund là phần capture còn chưa hoàn. Tổng `refund_lines` phải bằng `recommended_refund_brl`.

Confidence bắt đầu thấp và tăng theo các nhóm evidence độc lập: entity đã resolve, order record, domain evidence phù hợp issue, numeric total/timeline đầy đủ và policy evidence nếu case yêu cầu. Điểm bị trừ bởi source conflict và MCP failure, bị chặn ở `0.95`, ở `0.75` khi có conflict và ở `0.50` khi entity còn ambiguous.

### Lifecycle gate

`validate_artifacts` kiểm tra riêng từng case theo thứ tự:

```text
case_received → task_assigned → tool_result_consumed → handoff
→ policy_decided → verification_completed → case_finalized
```

`case_received` phải là event đầu, `case_finalized` phải là event cuối. Mỗi `evidence_ref` trong output phải xuất hiện trong một `tool_result_consumed` của cùng case; nếu thiếu hoặc sai thứ tự thì validation/package dừng ngay.

## 7. Khả năng tái lập

- Python >= 3.11; dependency ranges nằm trong `pyproject.toml`.
- State machine thuần Python async; xử lý case tuần tự ở CLI để trace và audit ổn định.
- Không dùng random cho quyết định nghiệp vụ; chỉ trace event ID dùng entropy.
- Giới hạn: 5 candidates, 2 resolved orders, 3 attempts cho mỗi tool/argument, 30 evidence refs.
- Chạy `pytest -q`, sau đó `day09 run`, `day09 validate`, và `day09 package --output dist/submission.zip`.
- API key chỉ đọc từ `.env`; không ghi vào trace, output hay gói nộp.
