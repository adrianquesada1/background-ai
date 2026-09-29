"""Identidad visual Llorca Group: grafito, rojo corporativo y fondo crema. Sin emojis."""
import streamlit as st

GRAFITO, ROJO, CREMA, LINEA, GRIS = "#2E2E2E", "#E1251B", "#FBFBF7", "#E3E1D8", "#77756E"

CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html, body, [class*="css"], .stMarkdown, .stText, button, input, textarea, select {{
    font-family: 'Inter', 'Segoe UI', Arial, sans-serif !important;
}}
.block-container {{ padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1500px; }}
h1 {{ font-weight: 700 !important; letter-spacing: -0.02em; color: {GRAFITO};
      border-left: 5px solid {ROJO}; padding-left: 0.7rem !important; margin-bottom: 0.4rem !important; }}
h2, h3 {{ font-weight: 600 !important; color: {GRAFITO}; letter-spacing: -0.01em; }}
[data-testid="stHeaderActionElements"] {{ display: none; }}
#MainMenu, [data-testid="stToolbar"] [data-testid="stAppDeployButton"], .stDeployButton {{ display: none !important; }}
[data-testid="stSidebar"] {{ background: #F4F3EE; border-right: 1px solid {LINEA}; }}
[data-testid="stSidebarNav"] a[aria-current="page"] {{ background: #FFFFFF; border-left: 3px solid {ROJO}; }}
[data-testid="stSidebarNav"] span {{ font-size: 0.92rem; }}
[data-testid="stMetric"] {{ background: #FFFFFF; border: 1px solid {LINEA}; border-radius: 6px; padding: 14px 16px 10px 16px; }}
[data-testid="stMetricLabel"] p {{ color: {GRIS}; font-size: 0.78rem !important; font-weight: 500; }}
[data-testid="stMetricValue"] {{ color: {GRAFITO}; font-weight: 600; font-size: 1.55rem !important; }}
.stTabs [data-baseweb="tab-list"] {{ gap: 1.4rem; border-bottom: 1px solid {LINEA}; }}
.stTabs [data-baseweb="tab"] {{ padding-left: 0; padding-right: 0; font-weight: 500; }}
.stButton button, .stDownloadButton button {{ border-radius: 4px; font-weight: 500; }}
[data-testid="stExpander"] {{ border: 1px solid {LINEA}; border-radius: 6px; background: #FFFFFF; }}
[data-testid="stDataFrame"] {{ border: 1px solid {LINEA}; border-radius: 6px; }}
div[data-testid="stVerticalBlockBorderWrapper"] {{ background: #FFFFFF; }}
.llorca-pie {{ color: {GRIS}; font-size: 0.75rem; margin-top: 0.5rem; }}
</style>
"""


def aplicar():
    st.markdown(CSS, unsafe_allow_html=True)
