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
- sales_revenue: Number (10,2) - 订单的销售金额（不含退款，等于 AMOUNT）
- actual_received_amount: Number (10,2) - 实际收到的金额（扣除退款/折扣后）
- sales_cost: Number (10,2) - 销售环节发生的成本（含运费、销售费用）
- purchase_cost: Number (10,2) - 商品采购/进货成本
- gross_profit: Number (10,2) - 毛利 = sales_revenue - purchase_cost
- net_profit: Number (10,2) - 净利润 = actual_received_amount - sales_cost