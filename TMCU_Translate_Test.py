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

# ===================== 1. 源码原始 UI & CSS 样式 [完全保留] =====================
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

# ===================== 2. 解析引擎 (集成修复方案) =====================

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
    """
    终极集成版：强制解析原始通道并解决 time 冲突
    """
    if not ASAMMDF_INSTALLED:
        st.error("❌ 环境缺失 asammdf 库。")
        return {}
    
    data_dict = {}
    tmp_mf4 = "temp_final.mf4"
    with open(tmp_mf4, "wb") as f:
        f.write(file_content)
    
    try:
        mdf = MDF(tmp_mf4)
        
        # 步骤 1: 尝试 DBC 解码 (源码逻辑)
        try:
            decoded = mdf.extract_bus_logging(database_files={'CAN': [(dbc_path, 0)]})
            # 转换为 DataFrame 避免 value2text 冲突
            df_dbc = decoded.to_dataframe(ignore_value2text_conversions=True)
            for col in df_dbc.columns:
                if col.lower() in ['t', 'time', 'timestamps']: continue
                sig = decoded.get(col)
                data_dict[col] = {
                    'x': sig.timestamps.tolist(),
                    'y': sig.samples.tolist(),
                    'unit': getattr(sig, 'unit', ""),
                    'label': col.split('.')[-1]
                }
        except:
            pass

        # 步骤 2: 强制扫描所有物理通道 (修复 Multiple occurrences 报错)
        # 遍历所有数据组(Groups)，通过精确索引锁定通道
        for g_idx, group in enumerate(mdf.groups):
            for c_idx, channel in enumerate(group.channels):
                name = channel.name
                if name.lower() in ['t', 'time', 'timestamps'] or name in data_dict:
                    continue
                # 排除系统级内部通道
                if any(x in name.lower() for x in ['vlinker', 'system', 'can_df']):
                    continue
                
                try:
                    # 核心修复：显式指明 group 和 index
                    sig = mdf.get(name=name, group=g_idx, index=c_idx)
                    clean_label = name.split('.')[-1]
                    data_dict[name] = {
                        'x': sig.timestamps.tolist(),
                        'y': sig.samples.tolist(),
                        'unit': getattr(sig, 'unit', ""),
                        'label': clean_label
                    }
                except:
                    continue
            if len(data_dict) > 300: break # 限制数量防止内存溢出
            
        mdf.close()
    except Exception as e:
        st.error(f"MF4 深度解析失败: {e}")
    finally:
        if os.path.exists(tmp_mf4): os.remove(tmp_mf4)
    return data_dict

def process_asc(file_content, db):
    """还原源码：带 J1939 掩码的 ASC 解析机制"""
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
                
                msg = None
                # 源码核心 J1939 掩码尝试
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

# ===================== 3. UI 布局与 Plotly 同步 [完全还原] =====================

st.title("🚗 HVFAN 移动端分析系统")

with st.sidebar:
    st.header("⚙️ 协议库配置")
    # 检测 asammdf 是否可用
    if not ASAMMDF_INSTALLED:
        st.error("⚠️ asammdf 依赖库加载失败")
        st.code("pip install asammdf pandas --user")
    
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
    file_key = f"final_v3_{uploaded_file.name}_{uploaded_file.size}"
    if 'data_cache' not in st.session_state or st.session_state.get('current_file_id') != file_key:
        with st.spinner('⏳ 正在进行深度解析(包含原始通道)...'):
            suffix = uploaded_file.name.split('.')[-1].lower()
            content = uploaded_file.read()
            if suffix in ['mf4', 'mdf']:
                st.session_state.data_cache = process_mf4(content, current_dbc_path)
            else:
                st.session_state.data_cache = process_asc(content, db)
            st.session_state.current_file_id = file_key
    
    full_data = st.session_state.data_cache

    if full_data:
        st.success(f"📈 成功解析 {len(full_data)} 个信号（含原始物理通道）")
        
        with st.expander("🛠️ 图表交互设置", expanded=True):
            all_sigs = sorted(full_data.keys())
            selected_sigs = st.multiselect("搜索并选择信号", all_sigs, default=all_sigs[:1] if all_sigs else [])
            c1, c2 = st.columns(2)
            with c1: sync_on = st.toggle("🔗 同步缩放", value=True)
            with c2: show_measure = st.toggle("📏 开启测量轴", value=True)

        if selected_sigs:
            charts_json = []
            for name in selected_sigs:
                d = full_data[name]
                x, y = d['x'], d['y']
                # 还原源码：15000 点抽稀算法
                if len(x) > 15000:
                    step = len(x) // 15000
                    x, y = x[::step], y[::step]
                charts_json.append({"id": f"chart_{hash(name)}", "title": f"{name} ({d['unit']})", "x": x, "y": y})

            # ===================== 源码核心 Plotly 同步脚本 [全还原] =====================
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
                        line: {{ width: 2, shape: 'hv', color: '#3498db' }} // 阶梯波形还原
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
        st.error("❌ 未发现匹配信号。请检查 DBC 协议与 MF4 是否匹配。")
