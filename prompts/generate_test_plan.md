Generate a Web2 internal-transfer plan using only the registered test DSL.
Do not generate Python or Playwright code, selectors, shell commands, URLs, or
any other executable content.

Allowed actions:
`login`, `open_internal_transfer`, `select_asset`, `fill_recipient`,
`fill_amount`, `submit`, `complete_security_verification`,
`refresh_transaction_history`.

Allowed assertions:
`page_loaded`, `validation_message_equals`, `request_not_sent`,
`transfer_request_succeeded`, `payer_balance_decreased`,
`recipient_balance_increased`, `transaction_record_created`,
`single_transaction_created`.

Dynamic source rules:
- `fill_recipient` may use only `recipient_account`.
- A normal dynamic amount may use `valid_transfer_amount`.
- An insufficient-balance amount must use
  `amount_above_available_balance`.
- Never place a dynamic source name in `value`.

Historical Bug source rules:
- Cite a retrieved historical Bug only as `BUG-<bug_id>`, for example
  `BUG-1227`.
- Use only Bug IDs present in the supplied validated `related_bugs`.
- Never invent a Bug ID and never treat an arbitrary `BUG-` prefix as trusted.

The following Golden Set is mandatory. Preserve each exact case ID, Chinese
title, P0 priority, and `人工基准:<case_id>` source:

- `TC-OTI-001`: `内部转账页面正常打开`; open the page and assert
  `page_loaded`.
- `TC-OTI-002`: `内部转账成功`; use `recipient_account`, use fixed value `10`,
  complete security verification before submit, and assert request success,
  both balance changes with amount `10`, and a transaction record.
- `TC-OTI-003`: `收款人不能为空`; submit an empty recipient and assert a
  required-field message plus `request_not_sent`.
- `TC-OTI-004`: `金额不能为空或为 0`; submit an empty or zero amount and
  assert the amount validation plus `request_not_sent`.
- `TC-OTI-005`: `余额不足时禁止转账`; use
  `amount_above_available_balance` and assert insufficient balance plus
  `request_not_sent`.
- `TC-OTI-006`: `重复点击提交只产生一笔交易`; use `recipient_account`, fixed
  value `10`, complete security verification, perform two consecutive
  `submit` actions, and assert `single_transaction_created`.

Additional evidence-based cases are allowed, but inferred cases must set
`inferred=true` and include a rationale. Return no more than 12 cases total,
merge duplicate coverage, and keep titles and descriptions concise. Return
only data matching the required structured schema.
