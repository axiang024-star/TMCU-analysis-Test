import streamlit as st
import cantools
import re
import os
import json
import io
import sys
import subprocess
import streamlit.components.v1 as components

# ===================== 0. 依赖库自动修复与加载 =====================
def ensure_dependencies():
    """检测并尝试安装缺失库，支持 --user 权限绕过"""
    try:
        from asammdf import MDF
        return True, ""
    except ImportError:
        try:
            # 针对权限受限环境（如 Linux adminuser 或 Python 3.14）
            subprocess.check_call([sys.executable, "-m", "pip", "install", "asammdf", "pandas", "--user"])
            from asammdf import MDF
            return True, ""
        except Exception as e:
            return False, str(e)

ASAMMDF_INSTALLED, ASAMMDF_ERROR = ensure_dependencies()

# ===================== 1. 核心配置与移动端 UI 增强 =====================
DBC_FILENAME = 'Geely_TMCU_V1.1_20250513_PrivateCAN_10.dbc'
st.set_page_config(page_title="HVFAN 移动端分析系统", layout="wide")

# 还原源码所有 CSS 补丁
st.markdown("""
    <style>
    .stFileUploader { position: relative; z-index: 1000 !important; }
    section[data-testid="stFileUploadDropzone"] {
        padding: 3rem 1rem !important;
        border: 2px dashed #3498db !important;
        background-color: #f0f7ff !important;
        border-radius: 15px;
    }
    @media (max-width: 768px) {
        .stMarkdown h1 { font-size: 1.2rem !important; }
        .st-emotion-cache-16idsys p { font-size: 13px !important; }
        .stMultiSelect div div { font-size: 12px !important; }
    }
    </style>
""", unsafe_allow_html=True)

# ===================== 2. 高性能解析引擎 =====================

@st.cache_resource
def load_dbc_engine(uploaded_file_content=None):
    """支持内建 DBC 和动态上传 DBC"""
    try:
        if uploaded_file_content is not None:
            dbc_text = uploaded_file_content.decode('gbk', errors='ignore')
            return cantools.database.load_string(dbc_text, strict=False)
        elif os.path.exists(DBC_FILENAME):
            return cantools.database.load_file(DBC_FILENAME, encoding='gbk', strict=False)
    except Exception as e:
        st.sidebar.error(f"DBC 解析失败: {e}")
    return None

def process_mf4(file_content, dbc_path):
    """还原：MF4 深度解析逻辑"""
    if not ASAMMDF_INSTALLED:
        st.error(f"❌ 环境缺失 asammdf 库。报错: {ASAMMDF_ERROR}")
        return {}
    
    from asammdf import MDF
    data_dict = {}
    tmp_mf4 = "temp_log.mf4"
    with open(tmp_mf4, "wb") as f:
        f.write(file_content)
    
    try:
        mdf = MDF(tmp_mf4)
        # 提取总线日志并关联 DBC 解析
        decoded = mdf.extract_bus_logging(database_files={'CAN': [(dbc_path, 0)]})
        df = decoded.to_dataframe()
        
        for col in df.columns:
            if ' ' in col or col.startswith('__'): continue
            sig_data = decoded.get(col)
            if sig_data is not None:
                data_dict[col] = {
                    'x': sig_data.timestamps.tolist(),
                    'y': sig_data.samples.tolist(),
                    'unit': sig_data.unit,
                    'label': col.split('.')[-1]
                }
        mdf.close()
    except Exception as e:
        st.error(f"MF4 解析失败: {e}")
    finally:
        if os.path.exists(tmp_mf4): os.remove(tmp_mf4)
    return data_dict

def process_asc(file_content, db):
    """还原：支持 J1939 模糊匹配的 ASC 解析"""
    data_dict = {}
    frame_re = re.compile(
        r'^\s*(?P<time>\d+\.\d+)\s+(?P<channel>\d+)\s+(?P<id>[0-9A-Fa-f]+)x\s+(?:Rx|Tx)\s+d\s+(?P<dlc>\d+)\s+(?P<data>(?:[0-9A-Fa-f]{2}\s*)+)', 
        re.MULTILINE
    )
    
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
                hex_data = m.group('data').strip().replace(' ', '')
                raw_payload = bytearray.fromhex(hex_data)
                
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
                            data_dict[full_n] = {
                                'x': [], 'y': [], 
                                'unit': sig_obj.unit or "", 
                                'label': s_n
                            }
                        data_dict[full_n]['x'].append(t)
                        data_dict[full_n]['y'].append(s_v)
            except: continue
    return data_dict

# ===================== 3. UI 布局与交互控制 =====================

st.title("🚗 HVFAN 移动端分析系统")

with st.sidebar:
    st.header("⚙️ 协议库配置")
    if not ASAMMDF_INSTALLED:
        st.error(f"⚠️ asammdf 依赖库加载失败")
        st.caption(f"错误细节: {ASAMMDF_ERROR}")
        st.info("尝试运行: pip install asammdf --user")
        
    uploaded_dbc = st.file_uploader("更新 DBC 文件", type=None, key="mobile_dbc_uploader")
    current_dbc_path = DBC_FILENAME
    if uploaded_dbc:
        with open("temp_proto.dbc", "wb") as f:
            f.write(uploaded_dbc.getvalue())
        current_dbc_path = "temp_proto.dbc"

# 加载 DBC 引擎
dbc_bytes = uploaded_dbc.getvalue() if uploaded_dbc else None
db = load_dbc_engine(dbc_bytes)

if not db:
    st.warning("⚠️ 协议库未就绪。请确认侧边栏 DBC 状态。")
    st.stop()

# 报文上传
uploaded_file = st.file_uploader("📂 上传报文 (支持 .asc / .mf4 / .txt)", type=None, key="mobile_data_uploader")

if uploaded_file is not None:
    file_key = f"cache_{uploaded_file.name}_{uploaded_file.size}"
    if 'data_cache' not in st.session_state or st.session_state.get('current_file_id') != file_key:
        with st.spinner('⏳ 正在解析大规模报文...'):
            suffix = uploaded_file.name.split('.')[-1].lower()
            content = uploaded_file.read()
            if suffix in ['mf4', 'mdf']:
                st.session_state.data_cache = process_mf4(content, current_dbc_path)
            else:
                st.session_state.data_cache = process_asc(content, db)
            st.session_state.current_file_id = file_key
    
    full_data = st.session_state.data_cache

    if not full_data:
        st.error("❌ 解析失败：未发现匹配的信号 ID 或文件格式不支持。")
    else:
        st.success(f"📈 成功识别 {len(full_data)} 个信号")

        # 还原：所有交互控制开关
        with st.expander("🛠️ 信号过滤与交互设置", expanded=True):
            all_sigs = sorted(full_data.keys())
            selected_sigs = st.multiselect("选择分析信号 (支持搜索)", all_sigs, default=all_sigs[:1] if all_sigs else [])
            
            c1, c2 = st.columns(2)
            with c1: sync_on = st.toggle("🔗 同步缩放", value=True)
            with c2: show_measure = st.toggle("📏 开启测量轴", value=True)

        if selected_sigs:
            charts_json = []
            for name in selected_sigs:
                d = full_data[name]
                x, y = d['x'], d['y']
                
                # 还原：大数据量自动抽稀优化（15000点）
                if len(x) > 15000:
                    step = len(x) // 15000
                    x, y = x[::step], y[::step]
                
                charts_json.append({
                    "id": f"ch_{hash(name)}", 
                    "title": f"{name} ({d['unit']})", 
                    "x": x, 
                    "y": y
                })

            # 还原：Plotly 同步缩放渲染引擎
            js_code = f"""
            <script src="https://cdn.plot.ly/plotly-2.24.1.min.js"></script>
            <div id="chart-box"></div>
            <script>
                const dataSet = {json.dumps(charts_json)};
                const sync = {str(sync_on).lower()};
                const showMeasure = {str(show_measure).lower()};
                const container = document.getElementById('chart-box');
                const chartIds = [];
                let relayouting = false;

                dataSet.forEach(data => {{
                    const d = document.createElement('div');
                    d.id = data.id;
                    d.style.marginBottom = '20px';
                    d.style.height = '320px';
                    container.appendChild(d);
                    chartIds.push(data.id);

                    const layout = {{
                        title: {{ text: data.title, font: {{ size: 14 }} }},
                        margin: {{ l: 50, r: 20, t: 40, b: 40 }},
                        template: 'plotly_white',
                        hovermode: showMeasure ? "x unified" : "closest",
                        xaxis: {{ showspikes: true, spikemode: 'across', spikedash: 'dot', spikecolor: '#999' }},
                        yaxis: {{ autorange: true }}
                    }};

                    Plotly.newPlot(data.id, [{{ 
                        x: data.x, 
                        y: data.y, 
                        type: 'scatter', 
                        mode: 'lines', 
                        line: {{ width: 2, color: '#1f77b4' }} 
                    }}], layout, {{ 
                        responsive: true, 
                        displaylogo: false, 
                        scrollZoom: true 
                    }});

                    if (sync) {{
                        document.getElementById(data.id).on('plotly_relayout', (ed) => {{
                            if (relayouting) return;
                            relayouting = true;
                            const up = {{}};
                            if (ed['xaxis.range[0]']) {{
                                up['xaxis.range[0]'] = ed['xaxis.range[0]'];
                                up['xaxis.range[1]'] = ed['xaxis.range[1]'];
                            }} else if (ed['xaxis.autorange']) {{
                                up['xaxis.autorange'] = true;
                            }}
                            if (Object.keys(up).length > 0) {{
                                const ps = chartIds.map(id => id !== data.id ? Plotly.relayout(id, up) : null);
                                Promise.all(ps).then(() => relayouting = false);
                            }} else relayouting = false;
                        }});
                    }}
                }});
            </script>
            """
            components.html(js_code, height=len(selected_sigs)*350 + 50, scrolling=False)
