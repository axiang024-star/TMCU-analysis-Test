import streamlit as st
import cantools
import re
import os
import json
import io
import sys
import streamlit.components.v1 as components

# ===================== 环境兼容性补丁 =====================
try:
    from asammdf import MDF
    ASAMMDF_INSTALLED = True
except ImportError:
    ASAMMDF_INSTALLED = False

# ===================== 1. 源码原始 UI 配置 =====================
DBC_FILENAME = 'Geely_TMCU_V1.1_20250513_PrivateCAN_10.dbc'
st.set_page_config(page_title="HVFAN 移动端分析系统", layout="wide")

# 还原源码最初的 CSS 注入
st.markdown("""
    <style>
    .stFileUploader { position: relative; z-index: 1000 !important; }
    section[data-testid="stFileUploadDropzone"] {
        padding: 3rem 1rem !important;
        border: 2px dashed #3498db !important;
        background-color: #f0f7ff !important;
        border-radius: 15px;
    }
    .stMultiSelect [data-baseweb="tag"] { background-color: #3498db !important; }
    </style>
""", unsafe_allow_html=True)

# ===================== 2. 源码解析逻辑 (含 MF4 云端修复) =====================

@st.cache_resource
def load_dbc_engine(uploaded_file_content=None):
    """完全保留源码的 DBC 加载逻辑"""
    try:
        if uploaded_file_content is not None:
            # 兼容上传的 DBC
            dbc_text = uploaded_file_content.decode('gbk', errors='ignore')
            return cantools.database.load_string(dbc_text, strict=False)
        elif os.path.exists(DBC_FILENAME):
            # 兼容本地默认 DBC
            return cantools.database.load_file(DBC_FILENAME, encoding='gbk', strict=False)
    except Exception as e:
        st.sidebar.error(f"DBC 解析错误: {e}")
    return None

def process_mf4(file_content, dbc_path):
    """在保留源码功能基础上，修复云端 MF4 解析"""
    if not ASAMMDF_INSTALLED:
        st.error("❌ 环境缺失 asammdf 库。")
        return {}
    
    data_dict = {}
    tmp_mf4 = "temp_log.mf4"
    with open(tmp_mf4, "wb") as f:
        f.write(file_content)
    
    try:
        mdf = MDF(tmp_mf4)
        # 还原源码：优先使用总线报文解析
        try:
            decoded = mdf.extract_bus_logging(database_files={'CAN': [(dbc_path, 0)]})
            df = decoded.to_dataframe()
        except:
            # 云端容错：直读信号通道
            df = mdf.to_dataframe(channels=[c for c in mdf.channels_db if not c.startswith('CAN_')][:100])

        if df is not None:
            for col in df.columns:
                if col.lower() in ['t', 'time', 'timestamps']: continue
                try:
                    sig = mdf.get(col)
                    data_dict[col] = {
                        'x': sig.timestamps.tolist(),
                        'y': sig.samples.tolist(),
                        'unit': getattr(sig, 'unit', ""),
                        'label': col.split('.')[-1]
                    }
                except: continue
        mdf.close()
    except Exception as e:
        st.error(f"MF4 解析报错: {e}")
    finally:
        if os.path.exists(tmp_mf4): os.remove(tmp_mf4)
    return data_dict

def process_asc(file_content, db):
    """完全保留源码的 ASC 解析 (含 J1939 掩码逻辑)"""
    data_dict = {}
    frame_re = re.compile(
        r'^\s*(?P<time>\d+\.\d+)\s+(?P<channel>\d+)\s+(?P<id>[0-9A-Fa-f]+)x\s+(?:Rx|Tx)\s+d\s+(?P<dlc>\d+)\s+(?P<data>(?:[0-9A-Fa-f]{2}\s*)+)', 
        re.MULTILINE
    )
    
    # 源码原始的多编码尝试逻辑
    text_data = ""
    for enc in ['utf-8', 'gbk', 'latin-1']:
        try:
            text_data = file_content.decode(enc, errors='ignore')
            if "Rx" in text_data or "Tx" in text_data: break
        except: continue
            
    lines = [l.strip() for l in text_data.splitlines() if l.strip()]
    for line in lines:
        m = frame_re.match(line)
        if m:
            try:
                t = float(m.group('time'))
                raw_id = int(m.group('id'), 16)
                raw_payload = bytearray.fromhex(m.group('data').strip().replace(' ', ''))
                
                # 源码核心：J1939 掩码匹配
                msg = None
                for search_id in [raw_id, raw_id & 0x1FFFFFFF, raw_id & 0x00FFFFFF]:
                    try:
                        msg = db.get_message_by_frame_id(search_id)
                        if msg: break
                    except KeyError: continue
                
                if msg:
                    if len(raw_payload) < msg.length:
                        raw_payload = raw_payload.ljust(msg.length, b'\x00')
                    decoded = msg.decode(raw_payload, decode_choices=False)
                    for s_n, s_v in decoded.items():
                        if not isinstance(s_v, (int, float)): continue
                        full_n = f"{msg.name}::{s_n}"
                        if full_n not in data_dict:
                            sig_obj = msg.get_signal_by_name(s_n)
                            data_dict[full_n] = {'x': [], 'y': [], 'unit': sig_obj.unit or "", 'label': s_n}
                        data_dict[full_n]['x'].append(t)
                        data_dict[full_n]['y'].append(s_v)
            except: continue
    return data_dict

# ===================== 3. UI 交互逻辑 (严格同步源码) =====================

st.title("🚗 HVFAN 移动端分析系统")

# 侧边栏保持源码风格
with st.sidebar:
    st.header("⚙️ 协议库配置")
    uploaded_dbc = st.file_uploader("更新 DBC 文件", type=None)
    current_dbc_path = DBC_FILENAME
    if uploaded_dbc:
        with open("temp_runtime.dbc", "wb") as f:
            f.write(uploaded_dbc.getvalue())
        current_dbc_path = "temp_runtime.dbc"

db = load_dbc_engine(uploaded_dbc.getvalue() if uploaded_dbc else None)
if not db:
    st.warning("请先配置协议库 (DBC)")
    st.stop()

uploaded_file = st.file_uploader("📂 上传报文 (.asc / .mf4)", type=None)

if uploaded_file is not None:
    file_key = f"src_{uploaded_file.name}_{uploaded_file.size}"
    if 'data_cache' not in st.session_state or st.session_state.get('current_file_id') != file_key:
        with st.spinner('⏳ 正在解析...'):
            suffix = uploaded_file.name.split('.')[-1].lower()
            content = uploaded_file.read()
            if suffix in ['mf4', 'mdf']:
                st.session_state.data_cache = process_mf4(content, current_dbc_path)
            else:
                st.session_state.data_cache = process_asc(content, db)
            st.session_state.current_file_id = file_key
    
    full_data = st.session_state.data_cache

    if full_data:
        st.success(f"📈 已加载 {len(full_data)} 个信号")
        
        # 还原源码：控制面板设置
        with st.expander("🛠️ 分析设置", expanded=True):
            all_sigs = sorted(full_data.keys())
            selected_sigs = st.multiselect("选择分析信号", all_sigs, default=all_sigs[:1] if all_sigs else [])
            c1, c2 = st.columns(2)
            with c1: sync_on = st.toggle("🔗 同步缩放", value=True)
            with c2: show_measure = st.toggle("📏 开启测量轴", value=True)

        if selected_sigs:
            charts_json = []
            for name in selected_sigs:
                d = full_data[name]
                x, y = d['x'], d['y']
                # 还原源码：15000 点抽稀逻辑
                if len(x) > 15000:
                    step = len(x) // 15000
                    x, y = x[::step], y[::step]
                charts_json.append({"id": f"ch_{hash(name)}", "title": f"{name} ({d['unit']})", "x": x, "y": y})

            # ===================== 源码最关键的 Plotly JS 还原 =====================
            js_code = f"""
            <script src="https://cdn.plot.ly/plotly-2.24.1.min.js"></script>
            <div id="plotly-container"></div>
            <script>
                const dataSet = {json.dumps(charts_json)};
                const syncEnabled = {str(sync_on).lower()};
                const hoverMode = "{'x unified' if show_measure else 'closest'}";
                const container = document.getElementById('plotly-container');
                const chartIds = [];
                let isRelayouting = false;

                dataSet.forEach(data => {{
                    const div = document.createElement('div');
                    div.id = data.id;
                    div.style.marginBottom = '15px';
                    container.appendChild(div);
                    chartIds.push(data.id);

                    const config = {{ responsive: true, displaylogo: false, scrollZoom: true }};
                    const layout = {{
                        title: {{ text: data.title, font: {{ size: 14 }} }},
                        margin: {{ l: 50, r: 20, t: 40, b: 40 }},
                        hovermode: hoverMode,
                        xaxis: {{ showspikes: true, spikemode: 'across', spikedash: 'dot', spikesnap: 'cursor' }},
                        yaxis: {{ autorange: true, fixedrange: false }}
                    }};

                    Plotly.newPlot(data.id, [{{
                        x: data.x,
                        y: data.y,
                        mode: 'lines',
                        line: {{ width: 2, shape: 'hv' }}  // 还原源码的阶梯线设置
                    }}], layout, config);

                    if (syncEnabled) {{
                        div.on('plotly_relayout', (eventData) => {{
                            if (isRelayouting) return;
                            isRelayouting = true;
                            const update = {{}};
                            if (eventData['xaxis.range[0]']) {{
                                update['xaxis.range[0]'] = eventData['xaxis.range[0]'];
                                update['xaxis.range[1]'] = eventData['xaxis.range[1]'];
                            }}
                            if (eventData['xaxis.autorange']) update['xaxis.autorange'] = true;
                            
                            if (Object.keys(update).length > 0) {{
                                chartIds.forEach(id => {{
                                    if (id !== data.id) Plotly.relayout(id, update);
                                }});
                            }}
                            isRelayouting = false;
                        }});
                    }}
                }});
            </script>
            """
            components.html(js_code, height=len(selected_sigs)*350 + 100, scrolling=True)
    else:
        st.error("未发现匹配信号，请检查 DBC 文件。")
