**Table 3: STREAM_HACKATHON.STREAMLIT.PAYMENTS** (Stores payment information)

This table contains information about payments made by customers for their orders, including the date and amount of each payment.

- PAYMENT_ID: Number (38,0) [Primary Key, Not Null] - Unique identifier for payments
- ORDER_ID: Number (38,0) [Foreign Key - ORDER_DETAILS(ORDER_ID)] - Associated order for the payment
- PAYMENT_DATE: Date - Date when the payment was made
- AMOUNT: Number (10,2) - Amount of the payment
- 收入: Number (10,2) - 收入金额（该笔支付的销售收入）
- 成本: Number (10,2) - 成本金额（该笔支付对应的销售成本）
- 毛利额: Number (10,2) - 毛利额（= 收入 - 成本）