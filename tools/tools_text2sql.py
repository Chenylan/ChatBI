"""自然语言 -> PostgreSQL 查询的工具，以及时区时间工具。

取代 tools/tools_text2sqlite.py：业务库迁到 PostgreSQL 后，生成的 SQL 改成 PG 方言，
并把 SQLite 特有的日期写法（DATE('now')）替换成 PG 的 CURRENT_DATE / INTERVAL。
"""
import datetime
import os
from typing import Any, Dict

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.tools import tool

load_dotenv()

llm = None
_current_model_name = None


def get_llm(model_name: str = "qwen-plus"):
    """获取或初始化语言模型（FastAPI 后端使用，不再依赖 Streamlit session_state）。"""
    global llm, _current_model_name
    current_model = model_name
    if llm is None or _current_model_name != current_model:
        llm = init_chat_model(
            model=current_model,
            model_provider="openai",
            base_url=os.getenv("OPENAI_API_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        )
        _current_model_name = current_model
    return llm


@tool(
    "text2sql_query",
    description="Use LLM to convert natural language text to a PostgreSQL query.",
)
def text2sql_tool(text: str, table_schema: str = "") -> Dict[str, Any]:
    """把自然语言转成 PostgreSQL 查询语句。

    参数:
        text: 自然语言描述
        table_schema: 可选，表结构信息（如有）
    返回:
        {"sql_query": 生成的 SQL}
    """
    def _build_prompt(text: str, table_schema: str) -> str:
        example_prompt = (
            "自然语言描述: 查询所有订单总金额大于1000元的客户姓名和订单号。\n"
            "表结构: "
            "Table 2: STREAM_HACKATHON.STREAMLIT.ORDER_DETAILS (Stores order information)"
            "This table contains information about orders placed by customers, including the date and total amount of each order."
            "ORDER_ID: Number (38,0) [Primary Key, Not Null] - Unique identifier for orders"
            "CUSTOMER_ID: Number (38,0) [Foreign Key - CUSTOMER_DETAILS(CUSTOMER_ID)] - Customer who made the order"
            "ORDER_DATE: Date - Date when the order was made"
            "TOTAL_AMOUNT: Number (10,2) - Total amount of the order"
            "输出SQL: SELECT COUNT(*) FROM order_details WHERE order_date >= CURRENT_DATE - INTERVAL '7 days';"
        )
        return (
            "你是一个数据库专家，请根据以下自然语言描述，生成一个 PostgreSQL 查询语句，只返回 SQL，不要有任何解释。\n"
            "注意：\n"
            "1. 输出 SQL 里的表名/列名用小写（如 order_details），不要带 STREAM_HACKATHON.STREAMLIT 前缀。\n"
            "2. 日期/时间比较用 CURRENT_DATE / CURRENT_TIMESTAMP，不要用 SQLite 的 DATE('now')。\n"
            f"自然语言描述: {text}\n"
            f"表结构: {table_schema}\n"
            f"示例: {example_prompt}\n"
        )

    prompt = _build_prompt(text, table_schema)
    current_llm = get_llm()
    response = current_llm.invoke(prompt)
    return {"sql_query": response.content}


@tool(
    "get_time_by_timezone",
    description="获取指定时区的当前时间，返回格式如 2025-09-06 15:30:00。参数 timezone 例如 'Asia/Shanghai'、'UTC'、'America/New_York'。",
)
def get_time_by_timezone(timezone: str = "Asia/Shanghai") -> Dict[str, Any]:
    """获取指定时区的当前时间。"""
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(timezone)
        now = datetime.datetime.now(tz)
        return {"current_time": now.strftime("%Y-%m-%d %H:%M:%S"), "timezone": timezone}
    except Exception as e:
        utc_now = datetime.datetime.utcnow()
        beijing_now = utc_now + datetime.timedelta(hours=8)
        return {
            "current_time": beijing_now.strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": "Asia/Shanghai",
            "error": str(e),
        }
