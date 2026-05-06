import streamlit as st
import cantools
import re
import os
import json
import io
import sys
import streamlit.components.v1 as components

# ===================== 环境依赖检测 =====================
try:
    from asammdf import MDF
    ASAMMDF_INSTALLED = True
except ImportError:
    ASAMMDF_INSTALLED = False

# ===================== 1. 源码原始 UI & CSS 配置 =====================
DBC_FILENAME = 'Geely_TMCU_V1.1_20250513_PrivateCAN_10.dbc'
st.set_page_config(page_title="HVFAN 移动端分析系统", layout="wide")

st.markdown("""
    <style>
    .stFileUploader { position: relative; z-index: 1000 !important; }
    section[data-testid="stFileUploadDropzone"] {
        padding: 3rem 1rem !important;
        border: 2px dashed #3498db !important;
        background-color: #f0f7ff !important;
        border-radius: 15px;
    }
    .stMultiSelect [data-baseweb="tag"] {
        background-color: #3498db !important;
        color: white !important;
    }
    @media (max-width: 768px) {
        .stMarkdown h1 { font-size: 1.5rem !important; }
    }
    </style>
""", unsafe_allow_html=True)

# ===================== 2. 解析引擎 (集成强力 MF4 扫描) =====================

@st.cache_resource
def load_dbc_engine(uploaded_file_content=None):
    """还原源码：支持内建与上传 DBC"""
    try:
        if uploaded_file_content is not None:
            dbc_text = uploaded_file_content.decode('gbk', errors='ignore')
            return cantools.database.load_string(dbc_text, strict=False)
        elif os.path.exists(DBC_FILENAME):
            return cantools.database.load_file(DBC_FILENAME, encoding='gbk', strict=False)
    except Exception as e:
        st.sidebar.error(f"DBC 解析错误: {e}")
    return None

def process_mf4(file_content, dbc_path):
    """增强版：强制读取原始通道 + DBC 匹配"""
    if not ASAMMDF_INSTALLED:
        st.error("❌ 环境缺失 asammdf 库。")
        return {}
    
    data_dict = {}
    tmp_mf4 = "temp_scan.mf4"
    with open(tmp_mf4, "wb") as f:
        f.write(file_content)
    
    try:
        mdf = MDF(tmp_mf4)
        
        # 步骤 1：尝试 DBC 解码（源码逻辑）
        try:
            decoded = mdf.extract_bus_logging(database_files={'CAN': [(dbc_path, 0)]})
            df_dbc = decoded.to_dataframe()
        except:
            df_dbc = None

        # 步骤 2：强制扫描 MF4 内部所有物理通道（新增修复逻辑）
        # 排除系统级内部通道，保留数据通道
        all_ch = [c for c in mdf.channels_db if not any(x in c.lower() for x in ['system', 'vlinker', 'can_df'])]
        df_raw = mdf.to_dataframe(channels=all_ch[:120]) # 限制数量防止内存溢出

        # 合并处理
        def add_to_dict(df_source):
            if df_source is not None and not df_source.empty:
                for col in df_source.columns:
                    if col.lower() in ['t', 'time', 'timestamps'] or col in data_dict: continue
                    try:
                        sig = mdf.get(col)
                        # 清洗显示名称：去掉复杂的总线前缀
                        clean_label = col.split('.')[-1] if '.' in col else col
                        data_dict[col] = {
                            'x': sig.timestamps.tolist(),
                            'y': sig.samples.tolist(),
                            'unit': getattr(sig, 'unit', ""),
                            'label': clean_label
                        }
                    except: continue

        add_to_dict(df_dbc)
        add_to_dict(df_raw)
        
        mdf.close()
    except Exception as e:
        st.error(f"MF4 深度解析失败: {e}")
    finally:
        if os.path.exists(tmp_mf4): os.remove(tmp_mf4)
    return data_dict

def process_asc(file_content, db):
    """还原源码：带 J1939 掩码的 ASC 解析"""
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
                raw_payload = bytearray.fromhex(m.group('data').strip().replace(' ', ''))
                
                # 源码核心 J1939 匹配逻辑
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

# ===================== 3. UI 布局与 Plotly 同步 (全还原) =====================

st.title("🚗 HVFAN 移动端分析系统")

with st.sidebar:
    st.header("⚙️ 协议库配置")
    uploaded_dbc = st.file_uploader("更新 DBC 文件", type=None)
    current_dbc_path = DBC_FILENAME
    if uploaded_dbc:
        with open("temp_live.dbc", "wb") as f:
            f.write(uploaded_dbc.getvalue())
        current_dbc_path = "temp_live.dbc"

db = load_dbc_engine(uploaded_dbc.getvalue() if uploaded_dbc else None)
if not db:
    st.warning("⚠️ 请先配置 DBC 协议库")
    st.stop()

uploaded_file = st.file_uploader("📂 上传报文 (.asc / .mf4)", type=None)

if uploaded_file is not None:
    file_key = f"final_{uploaded_file.name}_{uploaded_file.size}"
    if 'data_cache' not in st.session_state or st.session_state.get('current_file_id') != file_key:
        with st.spinner('⏳ 正在进行多维解析...'):
            suffix = uploaded_file.name.split('.')[-1].lower()
            content = uploaded_file.read()
            if suffix in ['mf4', 'mdf']:
                st.session_state.data_cache = process_mf4(content, current_dbc_path)
            else:
                st.session_state.data_cache = process_asc(content, db)
            st.session_state.current_file_id = file_key
    
    full_data = st.session_state.data_cache

    if full_data:
        st.success(f"📈 成功加载 {len(full_data)} 个信号（包含原始通道）")
        
        with st.expander("🛠️ 图表交互设置", expanded=True):
            all_sigs = sorted(full_data.keys())
            # 还原源码：默认选第一个信号
            selected_sigs = st.multiselect("搜索并选择信号", all_sigs, default=all_sigs[:1] if all_sigs else [])
            c1, c2 = st.columns(2)
            with c1: sync_on = st.toggle("🔗 同步缩放", value=True)
            with c2: show_measure = st.toggle("📏 开启测量轴", value=True)

        if selected_sigs:
            charts_json = []
            for name in selected_sigs:
                d = full_data[name]
                x, y = d['x'], d['y']
                # 还原源码：15000 点抽稀
                if len(x) > 15000:
                    step = len(x) // 15000
                    x, y = x[::step], y[::step]
                charts_json.append({"id": f"chart_{hash(name)}", "title": f"{name} ({d['unit']})", "x": x, "y": y})

            # ===================== 源码核心 Plotly 同步脚本 (全还原) =====================
            js_code = f"""
            <script src="https://cdn.plot.ly/plotly-2.24.1.min.js"></script>
            <div id="chart-list"></div>
            <script>
                const dataSet = {json.dumps(charts_json)};
                const sync = {str(sync_on).lower()};
                const hoverMode = "{'x unified' if show_measure else 'closest'}";
                const container = document.getElementById('chart-list');
                const chartIds = [];
                let syncing = false;

                dataSet.forEach(data => {{
                    const div = document.createElement('div');
                    div.id = data.id;
                    div.style.marginBottom = '15px';
                    div.style.height = '350px';
                    container.appendChild(div);
                    chartIds.push(data.id);

                    // 还原源码 Layout 配置
                    const layout = {{
                        title: {{ text: data.title, font: {{ size: 14 }} }},
                        margin: {{ l: 60, r: 30, t: 40, b: 40 }},
                        hovermode: hoverMode,
                        xaxis: {{ showspikes: true, spikemode: 'across', spikedash: 'dot', spikesnap: 'cursor' }},
                        yaxis: {{ autorange: true, fixedrange: false }},
                        plot_bgcolor: 'white'
                    }};

                    Plotly.newPlot(data.id, [{{
                        x: data.x, y: data.y, mode: 'lines',
                        line: {{ width: 2, shape: 'hv', color: '#3498db' }}
                    }}], layout, {{ responsive: true, displaylogo: false, scrollZoom: true }});

                    if (sync) {{
                        document.getElementById(data.id).on('plotly_relayout', (ed) => {{
                            if (syncing) return;
                            syncing = true;
                            const update = {{}};
                            if (ed['xaxis.range[0]']) {{
                                update['xaxis.range[0]'] = ed['xaxis.range[0]'];
                                update['xaxis.range[1]'] = ed['xaxis.range[1]'];
                            }} else if (ed['xaxis.autorange']) {{
                                update['xaxis.autorange'] = true;
                            }}
                            
                            if (Object.keys(update).length > 0) {{
                                chartIds.forEach(id => {{
                                    if (id !== data.id) Plotly.relayout(id, update);
                                }});
                            }}
                            syncing = false;
                        }});
                    }}
                }});
            </script>
            """
            components.html(js_code, height=len(selected_sigs)*370 + 50, scrolling=False)
    else:
        st.error("❌ 仍未发现匹配信号。请确认 DBC 文件与 MF4 文件是否对应。")
