# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ input/candidate resolution đến MCP investigation, specialist agents, conflict resolver, verifier, output và trace.

```text
Input → Entity Resolver → Coordinator → Specialists → Conflict Resolver → Verifier → Output
            │                              │                  │             │
            └──────────────────────────── MCP ────────────────┴──────────── Trace
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | Case candidates, customer id | Resolve one order or mark ambiguity; load customer context | Entity: `get_order`; customer: `get_customer_history` | Resolution handoff with rejected candidates |
| Coordinator | Case and specialist results | Assign bounded work, cache per case, assemble output | Permission guard enforces agent allowlists | Specialist handoffs and finalization |
| Order/product | Resolved order ids | Load order, items, sellers and product context | `get_order`, `get_order_items`, `get_sellers`, `get_product_context` | Order/item evidence refs |
| Shipment | Resolved order ids | Reconstruct delivery timeline and delay responsibility | `get_shipment_summary` | Shipment verdict |
| Payment/refund | Resolved order ids | Reconcile captures, payment timeline and refunds | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | Financial totals and payment verdict |
| Policy | Normalized issue | Apply operational policy to supported facts | `get_policy` | Action constraints |
| Conflict resolver | Specialist evidence | Keep unresolved conflicts explicit; never invent a winner | No additional tool by default | `data_conflicts` |
| Verifier | Candidate output and refs | Validate schema, scope, refs, bounds and consistency | No MCP tool | `verification_completed` then output |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

## 3. Entity resolution và A2A protocol

Candidate exact id được ưu tiên; candidate duy nhất có evidence hợp lệ được chấp nhận. Nhiều candidate chỉ được chọn khi cùng khớp customer, nếu không trạng thái là `ambiguous`. Confidence dưới 0.9 không được coi là exact resolution. Mọi handoff mang `case_id` qua gateway; mỗi tool tối đa một retry, tối đa 20 call/case và cache theo `(tool, arguments)`. Trace chỉ ghi sự kiện quan sát được, không ghi suy luận riêng.

## 4. Evidence và conflict lifecycle

Gateway validate mọi response bằng `mcp-evidence-response-v1.schema.json`. Workflow chỉ lưu `evidence_ref` do gateway cấp và emit `tool_result_consumed` khi dùng ref. Policy evidence được ghi thêm bằng `policy_decided`. Evidence được giữ trong cache của một case, không chia sẻ giữa case; thiếu hoặc conflict được biểu diễn bằng `insufficient_evidence`/`data_conflicts` thay vì đoán.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout | 1 retry | Không suy đoán; giữ thiếu bằng chứng | `handoff` / `MCP_UNAVAILABLE` |
| Entity not found/ambiguous | 0 | `not_found` hoặc `ambiguous` | `handoff` / `ENTITY_UNRESOLVED` |
| Source conflict | 0 | Giữ `data_conflicts`, chọn null nếu chưa đủ policy | `tool_result_consumed` |
| Invalid specialist result | 1 bounded retry | Bỏ kết quả lỗi, verifier chặn finalize | `verification_completed` |

Nêu query budget/cache strategy để tránh gọi lặp và quét rộng. Retry phải có giới hạn, idempotent và không biến missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Trước finalize kiểm tra: output schema; đúng `case_id`; ids thuộc evidence đã gọi; refs có format hợp lệ và không trộn case; resolution/rejected candidates không chồng nhau; confidence trong [0,1]; tiền không âm; refund không vượt refundable total khi có dữ liệu; action chỉ sinh khi có bằng chứng hỗ trợ.

Policy agent đọc `policy_version` từ input và dùng `rules[primary_issue]` để lấy `case_status`, `recommended_action`, `refund_brl` và `responsible_parties`; không tự đặt số tiền hoàn. Verifier phân biệt payment split hợp lệ với refund/mismatch, phân biệt seller delay với logistics delay từ event actor, và hạ confidence khi có conflict hoặc thiếu evidence. Claim nào không được evidence hỗ trợ được đánh dấu `unsupported`; claim được hỗ trợ mới được dùng để chọn primary issue.

Confidence được calibrate bảo thủ từ entity resolution, claim support, số evidence đã tiêu thụ và conflict penalty; giá trị bị chặn trong [0, 0.95]. Không có evidence đủ thì output luôn `needs_investigation`, refund bằng 0 và không phát action.

## 7. Reproducibility

Workflow dùng Python async, không dùng model hoặc random seed; dependency được pin theo ranges trong `pyproject.toml`; concurrency hiện tuần tự, tối đa 20 MCP calls/case và retry 1 lần/tool. Chạy `day09 run`, `day09 validate`, `day09 package --output dist/submission.zip`. API key chỉ nằm trong `.env`, không ghi vào tài liệu hoặc ZIP.
