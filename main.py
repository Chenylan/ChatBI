import re, base64, json, warnings
import streamlit as st
from agent import MessagesState, create_agent
from ui.sqlitechat_ui import StreamlitUICallbackHandler, message_func
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from tools.access_control import current_user

# 忽略 "invalid escape sequence" 类型的语法警告
warnings.filterwarnings("ignore", category=SyntaxWarning, message="invalid escape sequence.*")
warnings.filterwarnings("ignore")
chat_history = []


# 读取本地图片并转为 Base64
def get_local_image_base64(image_path):
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode()

# 本地图片路径（如 assets/logo.png）
# image_base64 = get_local_image_base64("assets/nvidia.svg")


gradient_text_html = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Poppins:wght@700;900&display=swap');

.nvidia-title {
    font-family: 'Poppins', sans-serif;
    font-weight: 900;
    font-size: 3.5em;
    color: #76B900;
    text-shadow: 1px 1px 3px rgba(0, 0, 0, 0.2);
    margin: 0;
    padding: 10px 0 0 0;
    text-align: center;
}
.sqlitechat-title {
    font-family: 'Poppins', sans-serif;
    font-weight: 900;
    font-size: 3em;
    background: linear-gradient(90deg, #ff6a00, #ee0979);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    text-shadow: 2px 2px 5px rgba(0, 0, 0, 0.3);
    margin: 0;
    padding: 20px 0 0 0;
    text-align: center;
}
</style>
<div class="nvidia-title">NVIDIA 2025 Hackathon</div>
<div class="sqlitechat-title">ChatDB-SQLite</div>
"""


st.markdown(gradient_text_html, unsafe_allow_html=True)

caption_text = \
"""
NVIDIA 2025 Hackathon 参赛项目 - <b style="color:blue;">来自 Hello Agent 团队</b><br>
这里可以用自然语言跟数据库交互，可以生成各种图形报告，可以看到 MCP 调用日志
"""
caption_text = \
"""
NVIDIA 2025 Hackathon 参赛项目 - <b>来自 Hello Agent 团队</b><br>
这里可以用自然语言跟数据库交互，可以生成各种图形报告，可以看到 MCP 调用日志
"""

st.caption(caption_text, unsafe_allow_html=True)

model_options = {
    "qwen-plus": "qwen-plus",
    "qwen-turbo": "qwen-turbo",
    "qwen3-max-preview": "qwen3-max-preview"
}

model = st.radio(
    "选择 AI 模型",
    options=list(model_options.keys()),
    format_func=lambda x: model_options[x],
    index=0,
    horizontal=True,
)
st.session_state["model"] = model

# 表级权限演示：选择「当前用户」来模拟登录身份。实际生产环境应改为从登录态
# （如 SSO / JWT）解析出 user_id，而不是让用户自己下拉选择。
user_options = {
    "admin（全部表）": "admin",
    "alice（订单/客户/商品/行为）": "alice",
    "bob（支付/交易/订单/商品）": "bob",
}
user_label = st.selectbox("当前用户（表级权限）", options=list(user_options.keys()), index=0)
current_user.set(user_options[user_label])  # 注入身份，供 execute_sql_query 做权限校验

if "assistant_response_processed" not in st.session_state:
    st.session_state["assistant_response_processed"] = True  # Initialize to True

if "toast_shown" not in st.session_state:
    st.session_state["toast_shown"] = False

if "rate-limit" not in st.session_state:
    st.session_state["rate-limit"] = False

# # Show the toast only if it hasn't been shown before
# if not st.session_state["toast_shown"]:
#     st.toast("The snowflake data retrieval is disabled for now.", icon="👋")
#     st.session_state["toast_shown"] = True

# Show a warning if the model is rate-limited
if st.session_state["rate-limit"]:
    st.toast("Probably rate limited.. Go easy folks", icon="⚠️")
    st.session_state["rate-limit"] = False

if st.session_state["model"] == "Deepseek R1":
    st.warning("Deepseek R1 is highly rate limited. Please use it sparingly", icon="⚠️")

INITIAL_MESSAGE = [
    {"role": "user", "content": "Hi!"},
    {
        "role": "assistant",
        "content": "我是 ChatBI 智能助手，已接入 SQLite，快来聊聊吧 🔍",
    },
]
config = {"configurable": {"thread_id": "42"}}

with open("ui/sidebar.md", "r") as sidebar_file:
    sidebar_content = sidebar_file.read()

with open("ui/styles.md", "r") as styles_file:
    styles_content = styles_file.read()


# 从session_state获取数据
chat_data = st.session_state
with st.sidebar:
    # st.header("开始提问吧")
    with st.expander("开始提问吧"):
        st.info("我有多少个产品类别，每个类别有多少产品，用柱状图和环形图呈现", icon="❓")
        st.info("画出订单的面积堆积图，按时间线排列", icon="❓")
        st.info("分析支付数据，画折线图，支付时间 作为 X 轴", icon="❓")
    # st.header("MCP 调用日志", divider="rainbow")
    def display_tool():
        if "tool_events" in st.session_state:
            if st.session_state["tool_events"] == []:
                st.info("暂无调用日志，开始你的提问吧 🚀")
            else:
                for i, msg in enumerate(st.session_state["tool_events"], 1):
                    with st.expander(f"MCP #{i}: {msg.name}"):
                        # st.code(msg.content, language="text")
                        st.subheader(f"call_id")
                        st.write(f"{msg.tool_call_id}")
                        st.subheader(f"return")
                        try:
                            # 尝试解析JSON
                            parsed_json = json.loads(msg.content)
                            st.json(parsed_json)
                        except json.JSONDecodeError as e:
                            st.write(f"{msg.content}")
        else:
            st.info("暂无调用日志，开始你的提问吧 🚀")

    # display_tool()

    col1, col2= st.columns([5,1])
    with col2:
        if st.button("清除", type="secondary"):
            st.session_state["tool_events"] = []
            st.rerun()
    with col1:
        st.header("MCP 调用日志", divider="rainbow")
    display_tool()

    # 显示当前会话状态中的所有键值对
    # st.write("当前会话状态:", st.session_state)
# st.sidebar.markdown(sidebar_content)

# selected_table = st.sidebar.selectbox(
#     "Select a table:", options=list(snow_ddl.ddl_dict.keys())
# )
# st.sidebar.markdown(f"### DDL for {selected_table} table")
# st.sidebar.code(snow_ddl.ddl_dict[selected_table], language="sql")

# Add a reset button
# if st.sidebar.button("Reset Chat"):
#     for key in st.session_state.keys():
#         del st.session_state[key]
#     st.session_state["messages"] = INITIAL_MESSAGE
#     st.session_state["history"] = []

# st.sidebar.markdown(
#     "**Note:** <span style='color:red'>The snowflake data retrieval is disabled for now.</span>",
#     unsafe_allow_html=True,
# )

st.write(styles_content, unsafe_allow_html=True)

# Initialize the chat messages history
if "messages" not in st.session_state.keys():
    st.session_state["messages"] = INITIAL_MESSAGE

if "history" not in st.session_state:
    st.session_state["history"] = []

if "model" not in st.session_state:
    st.session_state["model"] = model


# col1, col2 = st.columns([4, 1])  # Adjust column widths as needed

# with col1:
#     # Prompt for user input and save
#     if prompt := st.chat_input("请输入你的问题..."):
#         if len(prompt) > 500:
#             st.error("Input is too long! Please limit your message to 500 characters.")
#         else:
#             st.session_state.messages.append({"role": "user", "content": prompt})
#             st.session_state["assistant_response_processed"] = False  # Assistant response not yet processed
# with col2:
#     if st.button("渲染"):
#         st.rerun()

if prompt := st.chat_input("请输入你的问题..."):
    if len(prompt) > 500:
        st.error("Input is too long! Please limit your message to 500 characters.")
    else:
        st.session_state.messages.append({"role": "user", "content": prompt})
        st.session_state["assistant_response_processed"] = False

messages_to_display = st.session_state.messages.copy()
# if not st.session_state["assistant_response_processed"]:
#     # Exclude the last assistant message if assistant response not yet processed
#     if messages_to_display and messages_to_display[-1]["role"] == "assistant":
#         print("\n\nthis is messages_to_display \n\n", messages_to_display)
#         messages_to_display = messages_to_display[:-1]

for message in messages_to_display:
    message_func(
        message["content"],
        is_user=(message["role"] == "user"),
        is_df=(message["role"] == "data"),
        model=model,
    )

callback_handler = StreamlitUICallbackHandler(model)

react_graph = create_agent(callback_handler, st.session_state["model"])



def append_chat_history(question, answer):
    st.session_state["history"].append((question, answer))


def get_sql(text):
    sql_match = re.search(r"```sql\n(.*)\n```", text, re.DOTALL)
    return sql_match.group(1) if sql_match else None


def append_message(content, role="assistant"):
    """Appends a message to the session state messages."""
    if content.strip():
        st.session_state.messages.append({"role": role, "content": content})


def handle_sql_exception(query, conn, e, retries=2):
    # append_message("Uh oh, I made an error, let me try to fix it..")
    # error_message = (
    #     "You gave me a wrong SQL. FIX The SQL query by searching the schema definition:  \n```sql\n"
    #     + query
    #     + "\n```\n Error message: \n "
    #     + str(e)
    # )
    # new_query = chain({"question": error_message, "chat_history": ""})["answer"]
    # append_message(new_query)
    # if get_sql(new_query) and retries > 0:
    #     return execute_sql(get_sql(new_query), conn, retries - 1)
    # else:
    #     append_message("I'm sorry, I couldn't fix the error. Please try again.")
    #     return None
    pass


def execute_sql(query, conn, retries=2):
    if re.match(r"^\s*(drop|alter|truncate|delete|insert|update)\s", query, re.I):
        append_message("Sorry, I can't execute queries that can modify the database.")
        return None
    try:
        return conn.sql(query).collect()
    except SnowparkSQLException as e:
        return handle_sql_exception(query, conn, e, retries)


if (
    "messages" in st.session_state
    and st.session_state["messages"][-1]["role"] == "user"
    and not st.session_state["assistant_response_processed"]
):
    user_input_content = st.session_state["messages"][-1]["content"]

    if isinstance(user_input_content, str):
        # Start loading animation
        callback_handler.start_loading_message()

        # 记忆管理兼容：从 session_state 构造完整历史（原实现依赖 checkpointer 累积，现已移除）。
        # 跳过初始欢迎语，只保留真实的 user/assistant 消息。
        _initial_contents = {"Hi!", "我是 ChatBI 智能助手，已接入 SQLite，快来聊聊吧 🔍"}
        history = []
        for m in st.session_state["messages"]:
            content = m.get("content", "")
            if content in _initial_contents:
                continue
            role = m.get("role")
            if role == "user":
                history.append(HumanMessage(content=content))
            elif role == "assistant":
                history.append(AIMessage(content=content))

        state = MessagesState(messages=history)
        result = react_graph.invoke(state, config=config, debug=True)
        # st.sidebar.write(f"result: {result}")
        

        if result["messages"]:
            assistant_message = callback_handler.final_message
            append_message(assistant_message)
            st.session_state["assistant_response_processed"] = True
            st.session_state["tool_events"] = [msg for msg in result['messages'] if isinstance(msg, ToolMessage)] # 这样写每一次提问会清空，要累积的话就需要append
            # with st.sidebar:
            #     display_tool()

        import time
        time.sleep(1)  # slight delay to ensure UI is ready
        st.rerun()
    


if (
    st.session_state["model"] == "Mixtral 8x7B"
    and st.session_state["messages"][-1]["content"] == ""
):
    st.session_state["rate-limit"] = True

    # if get_sql(result):
    #     conn = SnowflakeConnection().get_session()
    #     df = execute_sql(get_sql(result), conn)
    #     if df is not None:
    #         callback_handler.display_dataframe(df)
    #         append_message(df, "data", True)



# import time
# time.sleep(2)  # slight delay to ensure UI is ready
# st.rerun()