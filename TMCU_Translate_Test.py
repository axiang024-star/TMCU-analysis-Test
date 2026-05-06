import streamlit as st
import cantools
import re
import os
import json
import io
import sys
import streamlit.components.v1 as components

# ===================== 环境兼容性检测 =====================
try:
    from asammdf import MDF
    ASAMMDF_INSTALLED = True
except ImportError:
    ASAMMDF_INSTALLED = False

# ===================== 1. 源码原始 UI & CSS 样式 =====================
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
        .stSelectbox label, .stMultiSelect label { font-size: 14px !important; }
    }
    </style>
""", unsafe_allow_html=True)

# ===================== 2. 源码解析引擎 (集成多组冲突修复) =====================

@st.cache_resource
def load_dbc_engine(uploaded_file_content=None):
    """完全还原源码：内建与动态上传双支持"""
    try:
        if uploaded_file_content is not None:
            dbc_text = uploaded_file_content.decode('gbk', errors='ignore')
            return cantools.database.load_string(dbc_text, strict=False)
        elif os.path.exists(DBC_FILENAME):
            return cantools.database.load_file(DBC_FILENAME, encoding='gbk', strict=False)
    except Exception as e:
        st.sidebar.error(f"DBC 引擎启动失败: {e}")
    return None

def process_mf4(file_content, dbc_path):
    """
    集成修复方案：保留源码 extract_bus_logging 逻辑，
    并增加基于 (group, index) 的强制扫描以解决 time 冲突问题
    """
    if not ASAMMDF_INSTALLED:
        st.error("❌ 运行环境缺失 asammdf 库。")
        return {}
    
    data_dict = {}
    tmp_mf4 = "temp_sync_engine.mf4"
    with open(tmp_mf4, "wb") as f:
        f.write(file_content)
    
    try:
        mdf = MDF(tmp_mf4)
        
        # 优先执行源码的 DBC 解码策略
        try:
            decoded = mdf.extract_bus_logging(database_files={'CAN': [(dbc_path, 0)]})
            # 使用 ignore_value2text_conversions 提高兼容性
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

        # 强制原始通道扫描策略（解决 image_923a7d.png 中的冲突）
        for g_idx, group in enumerate(mdf.groups):
            for c_idx, channel in enumerate(group.channels):
                name = channel.name
                if name.lower() in ['t', 'time', 'timestamps'] or name in data_dict:
                    continue
                if any(x in name.lower() for x in ['vlinker', 'system', 'can_df']):
                    continue
                
                try:
                    # 使用 group/index 锁定，彻底解决 Multiple occurrences 报错
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
            if len(data_dict) > 500: break # 安全阈值
            
        mdf.close()
    except Exception as e:
        st.error(f"MF4 深度引擎解析失败: {e}")
    finally:
        if os.path.exists(tmp_mf4): os.remove(tmp_mf4)
    return data_dict

def process_asc(file_content, db):
    """像素级还原源码：ASC 解析与 J1939 掩码匹配"""
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
                # 源码特有的 J1939 掩码匹配优先级
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

# ===================== 3. 业务流程与 Plotly 还原 =====================

st.title("🚗 HVFAN 移动端分析系统")

with st.sidebar:
    st.header("⚙️ 协议库配置")
    uploaded_dbc = st.file_uploader("更新 DBC 文件", type=None)
    current_dbc_path = DBC_FILENAME
    if uploaded_dbc:
        with open("temp_proto.dbc", "wb") as f:
            f.write(uploaded_dbc.getvalue())
        current_dbc_path = "temp_proto.dbc"

db = load_dbc_engine(uploaded_dbc.getvalue() if uploaded_dbc else None)
if not db:
    st.warning("⚠️ 协议库未就绪，请上传 DBC 文件")
    st.stop()

uploaded_file = st.file_uploader("📂 上传报文 (.asc / .mf4)", type=None)

if uploaded_file is not None:
    file_key = f"src_final_{uploaded_file.name}_{uploaded_file.size}"
    if 'data_cache' not in st.session_state or st.session_state.get('current_file_id') != file_key:
        with st.spinner('⏳ 正在执行源码解析链路...'):
            suffix = uploaded_file.name.split('.')[-1].lower()
            content = uploaded_file.read()
            if suffix in ['mf4', 'mdf']:
                st.session_state.data_cache = process_mf4(content, current_dbc_path)
            else:
                st.session_state.data_cache = process_asc(content, db)
            st.session_state.current_file_id = file_key
    
    full_data = st.session_state.data_cache

    if full_data:
        st.success(f"📈 成功识别 {len(full_data)} 个信号")
        
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
                # 源码还原：15000 点抽稀逻辑
                if len(x) > 15000:
                    step = len(x) // 15000
                    x, y = x[::step], y[::step]
                charts_json.append({"id": f"ch_{hash(name)}", "title": f"{name} ({d['unit']})", "x": x, "y": y})

            # ===================== 源码 Plotly 同步脚本像素级还原 =====================
            js_code = f"""
            <script src="https://cdn.plot.ly/plotly-2.24.1.min.js"></script>
            <div id="chart-container"></div>
            <script>
                const dataSet = {json.dumps(charts_json)};
                const syncEnabled = {str(sync_on).lower()};
                const hoverMode = "{'x unified' if show_measure else 'closest'}";
                const container = document.getElementById('chart-container');
                const chartIds = [];
                let isRelayouting = false;

                dataSet.forEach(data => {{
                    const div = document.createElement('div');
                    div.id = data.id;
                    div.style.marginBottom = '20px';
                    div.style.height = '350px';
                    container.appendChild(div);
                    chartIds.push(data.id);

                    // 还原源码 Layout：包含 SpikeLine、Grid 和 Font 细节
                    const layout = {{
                        title: {{ text: data.title, font: {{ size: 14, color: '#2c3e50' }} }},
                        margin: {{ l: 60, r: 30, t: 50, b: 50 }},
                        hovermode: hoverMode,
                        xaxis: {{ 
                            showgrid: true, gridcolor: '#ecf0f1',
                            showspikes: true, spikemode: 'across', spikedash: 'dot', spikesnap: 'cursor' 
                        }},
                        yaxis: {{ 
                            showgrid: true, gridcolor: '#ecf0f1',
                            autorange: true, fixedrange: false 
                        }},
                        plot_bgcolor: 'rgba(0,0,0,0)',
                        paper_bgcolor: 'rgba(0,0,0,0)'
                    }};

                    const config = {{ 
                        responsive: true, 
                        displaylogo: false, 
                        scrollZoom: true,
                        modeBarButtonsToRemove: ['lasso2d', 'select2d']
                    }};

                    Plotly.newPlot(data.id, [{{
                        x: data.x,
                        y: data.y,
                        mode: 'lines',
                        line: {{ width: 2, shape: 'hv', color: '#3498db' }} // 源码阶梯波形还原
                    }}], layout, config);

                    // 还原源码：同步缩放核心逻辑
                    if (syncEnabled) {{
                        div.on('plotly_relayout', (eventData) => {{
                            if (isRelayouting) return;
                            isRelayouting = true;
                            const update = {{}};
                            if (eventData['xaxis.range[0]']) {{
                                update['xaxis.range[0]'] = eventData['xaxis.range[0]'];
                                update['xaxis.range[1]'] = eventData['xaxis.range[1]'];
                            }} else if (eventData['xaxis.autorange']) {{
                                update['xaxis.autorange'] = true;
                            }}
                            
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
            components.html(js_code, height=len(selected_sigs)*380 + 100, scrolling=False)
    else:
        st.error("❌ 未发现可解析信号，请检查 DBC 文件是否匹配。")
