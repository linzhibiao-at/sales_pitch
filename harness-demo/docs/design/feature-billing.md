---
last_updated: 2026-03-28
status: active          # active | deprecated | draft
owner: @Jack
---

# 计费功能设计

## 目标

计费模块负责套餐、订单、支付状态和账单记录管理，为企业用户提供可追踪的计费链路。

## 范围

包含：

- 套餐定义。
- 订单创建。
- 支付状态更新。
- 账单查询。
- 计费错误码。

不包含：

- 真实支付渠道接入，除非当前迭代明确要求。
- 财务报表和税务处理。

## 分层设计

- domain: 套餐、订单、支付状态、账单状态。
- mapper: 订单、账单、套餐数据库访问。
- service: 下单、状态变更、账单生成。
- controller: 订单和账单 API。
- config: 外部支付 `ApiClient` Bean 装配。

## 外部调用

支付渠道调用必须通过 `ApiClient` 抽象，不允许直接使用 `RestTemplate` 或 `HttpURLConnection`。

## 状态流转

```text
CREATED -> PAYING -> PAID
CREATED -> CANCELED
PAYING -> FAILED
FAILED -> PAYING
PAID -> REFUNDED
```

状态变更必须幂等，重复通知不能造成重复入账。

## 测试要求

- 创建订单。
- 支付成功。
- 支付失败。
- 重复支付通知。
- 订单不存在。
- 外部支付服务失败。
