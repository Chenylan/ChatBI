---
title: PAYMENTS 支付表结构及指标口径
category: schema
---

**Table 3: STREAM_HACKATHON.STREAMLIT.PAYMENTS** (Stores payment information)

This table contains information about payments made by customers for their orders, including the date, amount, and several revenue/cost/profit metrics at different granularities.

- PAYMENT_ID: Number (38,0) [Primary Key, Not Null] - Unique identifier for payments
- ORDER_ID: Number (38,0) [Foreign Key - ORDER_DETAILS(ORDER_ID)] - Associated order for the payment
- PAYMENT_DATE: Date - Date when the payment was made
- AMOUNT: Number (10,2) - Amount of the payment
- 销售收入: Number (10,2) - 订单的销售金额（不含退款，等于 AMOUNT）
- 实收金额: Number (10,2) - 实际收到的金额（扣除退款/折扣后）
- 销售成本: Number (10,2) - 销售环节发生的成本（含运费、销售费用）
- 采购成本: Number (10,2) - 商品采购/进货成本
- 毛利额: Number (10,2) - 毛利 = 销售收入 - 采购成本
- 净利润: Number (10,2) - 净利润 = 实收金额 - 销售成本