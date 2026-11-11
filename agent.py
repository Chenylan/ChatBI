from dataclasses import dataclass
from typing import Annotated, Sequence, Optional
import os
import warnings

from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.messages import SystemMessage, AIMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import START, END, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from tools.tool_executor import AgentToolExecutor
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage

from memory_manager import trim_messages

from tools.tools_rag import retriever_tool, search
from tools.tools_text2sql import text2sql_tool, get_time_by_timezone
from tools.tools_execute_sql import execute_sql_query
from tools.tools_charts import highcharts_tool
from tools.tools_export import export_artifacts_tool
from metrics.definitions import detect_ambiguous, format_metric_dict


from langchain_mcp_adapters.client import MultiServerMCPClient
import asyncio

from PIL import Image
from io import BytesIO

from dotenv import load_dotenv
# 加载 .env 文件，但不覆盖已存在的系统环境变量
load_dotenv(override=False)

# 抑制 Streamlit 在非 Streamlit 环境中的警告
# 这些警告在 FastAPI 后端环境中是正常的，可以安全忽略
warnings.filterwarnings("ignore", category=UserWarning, module="streamlit")
warnings.filterwarnings("ignore", message=".*missing ScriptRunContext.*")
warnings.filterwarnings("ignore", message=".*Session state does not function.*")
os.environ["STREAMLIT_SERVER_RUNNING"] = "false"

@dataclass
class MessagesState:
    messages: Annotated[Sequence[BaseMessage], add_messages]

# Set up MCP client
import os
from pathlib import Path

# 获取项目根目录
project_root = Path(__file__).resolve().parent
mcp_time_path = project_root / "tools" / "mcp_time.py"

client = MultiServerMCPClient(
    {
        "time": {
            "command": "python",
            # 使用绝对路径
            "args": [str(mcp_time_path)],
            "transport": "stdio",
        },
        # 注释掉失效的 fetch 服务
        # "fetch": {
        #     "transport": "streamable_http",
        #     "url": "https://mcp.api-inference.modelscope.net/12c7b43a064846/mcp"
        # }
    }
)
# 异步方式
async def get_mcp_tools():
    try:
        mcp_tools = await client.get_tools()
        return mcp_tools
    except Exception as e:
        print(f"Warning: Failed to load MCP tools: {e}")
        return []


# 安全地加载 MCP 工具，失败时不阻塞启动
try:
    mcp_tools = asyncio.run(get_mcp_tools())
except Exception as e:
    print(f"Warning: Failed to initialize MCP tools: {e}")
    mcp_tools = []
tools = [retriever_tool, search, text2sql_tool, highcharts_tool, execute_sql_query, export_artifacts_tool]
tools = tools + mcp_tools

@dataclass
class ModelConfig:
    model_name: str
    api_key: str
    base_url: Optional[str] = None

def get_env_var(var_name: str, default: Optional[str] = None) -> Optional[str]:
    """
    从系统环境变量或 .env 文件获取环境变量
    优先使用系统环境变量（如果已设置）
    """
    # os.getenv() 会先从系统环境变量读取，如果没有再从 .env 文件读取（如果 load_dotenv 已调用）
    value = os.getenv(var_name, default)
    return value

# 模型配置 - 使用函数动态获取环境变量，而不是在模块加载时
def get_model_configurations():
    """动态获取模型配置，确保每次调用时都读取最新的环境变量"""
    api_key = get_env_var("OPENAI_API_KEY")
    base_url = get_env_var("OPENAI_API_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    
    return {
        "qwen-plus": ModelConfig(
            model_name="qwen-plus", 
            api_key=api_key,
            base_url=base_url
        ),
        "qwen-turbo": ModelConfig(
            model_name="qwen-turbo", 
            api_key=api_key,
            base_url=base_url
        ),
        "qwen3-max-preview": ModelConfig(
            model_name="qwen3-max-preview", 
            api_key=api_key,
            base_url=base_url
        )
    }

_BASE_SYS_PROMPT = """You're an AI assistant specializing in data analysis with PostgreSQL SQL.
    # You MUST answer data questions by actually calling the tools — never describe what you would do.
    # 禁止输出"让我执行SQL查询…"这类只描述不做事的占位语；一旦需要查数，必须立刻调用
    ##在输出sql查询语句时候不要用别名，例如只输出SELECT COUNT(*) FROM ORDER_DETAILS;不要写成SELECT COUNT(*) AS order_count FROM ORDER_DETAILS;
    # execute_sql_query（需要时先用 database_schema_rag 查 schema、用 text2sql_query 生成 SQL）。
    Remember first get the schema of the table by using the tool "database_schema_rag" if needed.
    You have access to the following tools:
    - database_schema_rag: This tool allows you to search for database schema details when needed to generate the SQL code.
    - text2sql_query: This tool allows you to convert natural language text to a PostgreSQL query.
    - execute_sql_query: This tool allows you to execute a PostgreSQL query on a fixed database and return the results as JSON. Use this tool to query the database.
    - high_charts_json: This tool allows you to generate Highcharts JSON config from a list of numbers and chart type. IMPORTANT: When the user asks to draw a chart, graph, or visualization (like "画图", "画出", "图表", "可视化"), you MUST:
      1. First execute a SQL query to get the data
      2. Extract the numeric data from the query results
      3. Call high_charts_json tool with the numbers and appropriate chart type (like "area", "line", "column", "bar", "spline")
      4. Include the chart configuration in your final answer

    Your final answer should contain the analysis results or visualizations based on the user's question and the data retrieved from the database.
    When the user requests a chart, you MUST generate and include the chart configuration using the high_charts_json tool.
    You should try to add some insights based on the data.

    IMPORTANT — 最终答案的呈现格式（用于防止凭空编造数据）：
    每次回答数据类问题时，最终答案必须按以下顺序分节列出，缺一不可：
    1. 数据来源表：列出本次查询实际用到的表名（FROM / JOIN 涉及的所有表）。
    2. 计算过程：粘贴你实际执行、且由 execute_sql_query 工具返回的 SQL 语句。
    3. 查询结果：列出工具返回的原始数据（关键行或数值）。
    4. 结论：基于上述结果得出的解读。
    硬性要求：
    - 结论中的每一个数字都必须能追溯到第 3 步的查询结果，禁止编造查询结果之外的数字。
    - 若查询没有返回数据（rows 为空），必须明确说明"没有查到数据"，不得编造结果。
    """


def build_sys_msg() -> SystemMessage:
    """构建系统提示：基础指令 + 当前（可自定义的）指标口径字典。

    每次调用都重新读取口径字典，这样前端「指标口径」页面里的增删改会实时生效。
    """
    return SystemMessage(content=_BASE_SYS_PROMPT + "\n\n" + format_metric_dict())


def check_ambiguity(state: MessagesState) -> str:
    """在模型执行前，检查最新一条用户消息是否含模糊口径。"""
    for m in reversed(state.messages):
        if isinstance(m, HumanMessage):
            return "clarify" if detect_ambiguous(m.content) else "llm_agent"
    return "llm_agent"


def clarify(state: MessagesState) -> dict:
    """列出所有候选口径并结束本轮，等用户选择后再算。"""
    for m in reversed(state.messages):
        if isinstance(m, HumanMessage):
            ambiguous = detect_ambiguous(m.content)
            if ambiguous:
                lines = ["您的问题里以下指标有多个口径，为避免算错，请先确认具体口径："]
                for trigger, candidates in ambiguous.items():
                    lines.append(f"\n【{trigger}】")
                    for c in candidates:
                        lines.append(f"- {c.canonical}：{c.formula}")
                lines.append("\n请回复具体口径名称（如「销售毛利率」「销售收入」），我再继续计算。")
                return {"messages": [AIMessage(content="\n".join(lines))]}
    return {"messages": [AIMessage(content="请说明要计算的指标口径。")]}


def create_agent(callback_handler: BaseCallbackHandler, model_name: str, memory_prefix: str = "") -> StateGraph:
    # 动态获取模型配置，确保读取最新的环境变量
    model_configurations = get_model_configurations()
    config = model_configurations.get(model_name)
    if not config:
        raise ValueError(f"Unsupported model name: {model_name}")

    if not config.api_key:
        # 提供更详细的错误信息
        env_key = os.getenv("OPENAI_API_KEY")
        if env_key:
            raise ValueError(f"API key for model '{model_name}' is empty. Please check your environment variable OPENAI_API_KEY.")
        else:
            raise ValueError(
                f"API key for model '{model_name}' is not set. "
                f"Please set the OPENAI_API_KEY environment variable. "
                f"You can set it in your system environment or create a .env file in the project root."
            )

    llm = ChatOpenAI(
        model=config.model_name,
        api_key=config.api_key,
        callbacks=[callback_handler],
        streaming=True,
        base_url=config.base_url,
        temperature=0.1
    )

    llm_with_tools = llm.bind_tools(tools)

    def llm_agent(state: MessagesState):
        # 记忆管理：进 LLM 前做上下文剪裁（trim_messages），并注入记忆前缀（滚动摘要 + 召回的关键信息）
        context = [build_sys_msg()]
        if memory_prefix:
            context.append(SystemMessage(content=memory_prefix))
        context += trim_messages(state.messages)
        return {"messages": [llm_with_tools.invoke(context)]}

    builder = StateGraph(MessagesState)
    builder.add_node("clarify", clarify)
    builder.add_node("llm_agent", llm_agent)
    builder.add_node("tools", AgentToolExecutor(tools, max_retries=3))

    builder.add_conditional_edges(
        START, check_ambiguity, {"clarify": "clarify", "llm_agent": "llm_agent"}
    )
    builder.add_edge("clarify", END)
    builder.add_conditional_edges("llm_agent", tools_condition)
    builder.add_edge("tools", "llm_agent")
    # builder.add_edge("llm_agent", END)
    # 注意：不再使用 checkpointer（进程内存、易失）。多轮记忆由 memory_manager.MemoryManager
    # 显式加载/持久化，这里编译成纯 "llm_agent ⇄ tools" 循环即可。
    react_graph = builder.compile()

    # png_data = react_graph.get_graph(xray=True).draw_mermaid_png()
    # with open("graph_2.png", "wb") as f:
    #     f.write(png_data)

    # image = Image.open(BytesIO(png_data))
    # st.image(image, caption="React Graph")

    return react_graph
