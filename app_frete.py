import time
import streamlit as st
import pandas as pd
import numpy as np
import gspread
from gspread.exceptions import APIError
from google.oauth2.service_account import Credentials
import plotly.express as px
import plotly.graph_objects as go
from pathlib import Path
import frete_calc
from frete_calc import TRANSPORTADORAS, calcular_frete_medio, ufs_cobertas_por

st.set_page_config(
    page_title="Simulador de Frete — Liven",
    page_icon="📦",
    layout="wide",
)

SHEET_ID = "1_C0LlbksmGwp-Mu7j-85YFemyysN52M79fDGA-zyRpA"
CREDS_PATH = Path(__file__).parent / "credentials.json"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _sheets_client():
    creds = Credentials.from_service_account_file(CREDS_PATH, scopes=SCOPES)
    return gspread.authorize(creds)


def _fetch_worksheet(gc, sheet_name: str):
    for tentativa in range(4):
        try:
            return gc.open_by_key(SHEET_ID).worksheet(sheet_name).get_all_values()
        except APIError:
            if tentativa == 3:
                raise
            wait = 2 ** tentativa
            st.toast(f"Google Sheets indisponível, tentando novamente em {wait}s…")
            time.sleep(wait)


def _to_numeric(df, cols):
    for col in cols:
        if col in df.columns:
            df[col] = pd.to_numeric(
                df[col].astype(str).str.replace(".", "", regex=False).str.replace(",", ".", regex=False),
                errors="coerce",
            )
    return df


@st.cache_data(ttl=3600, show_spinner="Carregando dados da planilha…")
def load_data() -> pd.DataFrame:
    rows = _fetch_worksheet(_sheets_client(), "custo_fixo")
    header, data_rows = rows[0], rows[1:]
    df = pd.DataFrame(data_rows, columns=header)

    df = _to_numeric(df, ["VALOR_LIQUIDO_ITEM", "VALOR_FRETE", "PRECO_VENDA", "PESO_CAIXA",
                          "PESO_PRODUTO", "SUBCATEGORIA_ID", "ratio_frete"])

    df["DATA_RECEBIMENTO"] = pd.to_datetime(df["DATA_RECEBIMENTO"], errors="coerce")
    mask_ratio = (df["VALOR_LIQUIDO_ITEM"] > 0) & (df["VALOR_FRETE"] > 0)
    df["ratio_frete"] = None
    df.loc[mask_ratio, "ratio_frete"] = (
        df.loc[mask_ratio, "VALOR_FRETE"] / df.loc[mask_ratio, "VALOR_LIQUIDO_ITEM"]
    )
    df["ratio_frete"] = pd.to_numeric(df["ratio_frete"], errors="coerce")
    return df


@st.cache_data(ttl=3600, show_spinner="Carregando estoque…")
def load_estoque() -> pd.DataFrame:
    rows = _fetch_worksheet(_sheets_client(), "estoque")
    header, data_rows = rows[0], rows[1:]
    df = pd.DataFrame(data_rows, columns=header)
    df = _to_numeric(df, ["QUANTIDADE", "CUSTO_COMERCIAL", "PESO_PRODUTO", "PRECO_VENDA"])
    return df


# ── Carrega dados ──────────────────────────────────────────────────────────────
df = load_data()
df_estoque = load_estoque()

# ── Sidebar — filtros globais ─────────────────────────────────────────────────
st.sidebar.header("Filtros globais")

if st.sidebar.button("🔄 Atualizar dados"):
    st.cache_data.clear()
    st.rerun()

categorias = sorted(df["NOME_CATEGORIA"].dropna().unique())
sel_cat = st.sidebar.multiselect("Categorias", categorias, default=categorias)

if df["DATA_RECEBIMENTO"].notna().any():
    dmin = df["DATA_RECEBIMENTO"].min().date()
    dmax = df["DATA_RECEBIMENTO"].max().date()
    sel_periodo = st.sidebar.date_input("Período", value=(dmin, dmax), min_value=dmin, max_value=dmax)
    if len(sel_periodo) == 2:
        df = df[(df["DATA_RECEBIMENTO"].dt.date >= sel_periodo[0]) & (df["DATA_RECEBIMENTO"].dt.date <= sel_periodo[1])]

df_f = df[df["NOME_CATEGORIA"].isin(sel_cat)].copy()

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab1, tab2, tab3 = st.tabs(["📊 Diagnóstico", "🎛️ Simulador", "🏆 Recomendações"])


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1 — DIAGNÓSTICO
# ═══════════════════════════════════════════════════════════════════════════════
with tab1:
    st.title("Diagnóstico de Frete")
    st.caption("Visão geral do impacto do frete na base de vendas.")

    # KPIs
    frete_total = df_f["VALOR_FRETE"].sum()
    venda_total = df_f["VALOR_LIQUIDO_ITEM"].sum()
    ratio_medio = df_f["ratio_frete"].mean()
    linhas_criticas = (df_f["ratio_frete"] > 0.15).sum()
    frete_critico = df_f.loc[df_f["ratio_frete"] > 0.15, "VALOR_FRETE"].sum()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Frete total", f"R$ {frete_total:,.0f}")
    c2.metric("Valor líquido total", f"R$ {venda_total:,.0f}")
    c3.metric("Ratio médio geral", f"{ratio_medio:.1%}")
    c4.metric("Frete crítico (ratio >15%)", f"R$ {frete_critico:,.0f}", f"{linhas_criticas} linhas")

    st.divider()

    col_a, col_b = st.columns(2)

    # Gráfico 1 — ratio médio por categoria
    with col_a:
        cat_agg = (
            df_f.groupby("NOME_CATEGORIA")
            .agg(ratio_medio=("ratio_frete", "mean"), frete_total=("VALOR_FRETE", "sum"))
            .reset_index()
            .sort_values("ratio_medio", ascending=False)
        )
        fig1 = px.bar(
            cat_agg,
            x="ratio_medio",
            y="NOME_CATEGORIA",
            orientation="h",
            text=cat_agg["ratio_medio"].map("{:.1%}".format),
            color="ratio_medio",
            color_continuous_scale="RdYlGn_r",
            title="Ratio médio por categoria",
        )
        fig1.update_layout(coloraxis_showscale=False, yaxis_title="", xaxis_tickformat=".0%")
        st.plotly_chart(fig1, use_container_width=True)

    # Gráfico 2 — scatter frete vs valor
    with col_b:
        prod_agg = (
            df_f.groupby(["PRODUTO_ID", "NOME_PRODUTO", "NOME_CATEGORIA"])
            .agg(
                ocorrencias=("VALOR_FRETE", "count"),
                frete_medio=("VALOR_FRETE", "mean"),
                valor_medio=("VALOR_LIQUIDO_ITEM", "mean"),
                ratio_medio=("ratio_frete", "mean"),
            )
            .reset_index()
        )
        fig2 = px.scatter(
            prod_agg[prod_agg["ocorrencias"] >= 3],
            x="valor_medio",
            y="frete_medio",
            color="ratio_medio",
            size="ocorrencias",
            hover_name="NOME_PRODUTO",
            hover_data={"NOME_CATEGORIA": True, "ratio_medio": ":.1%", "ocorrencias": True},
            color_continuous_scale="RdYlGn_r",
            title="Produtos: Valor × Frete (tamanho = ocorrências)",
        )
        fig2.update_layout(xaxis_title="Valor médio (R$)", yaxis_title="Frete médio (R$)")
        st.plotly_chart(fig2, use_container_width=True)

    # Tabela de categorias
    st.subheader("Resumo por categoria")
    cat_full = (
        df_f.groupby("NOME_CATEGORIA")
        .agg(
            linhas=("VALOR_FRETE", "count"),
            frete_total=("VALOR_FRETE", "sum"),
            frete_medio=("VALOR_FRETE", "mean"),
            valor_total=("VALOR_LIQUIDO_ITEM", "sum"),
            ratio_medio=("ratio_frete", "mean"),
            ratio_mediana=("ratio_frete", "median"),
        )
        .assign(frete_pct_receita=lambda x: x["frete_total"] / x["valor_total"])
        .reset_index()
        .sort_values("ratio_medio", ascending=False)
    )
    st.dataframe(
        cat_full.style.format(
            {
                "frete_total": "R$ {:,.0f}",
                "frete_medio": "R$ {:,.2f}",
                "valor_total": "R$ {:,.0f}",
                "ratio_medio": "{:.1%}",
                "ratio_mediana": "{:.1%}",
                "frete_pct_receita": "{:.1%}",
            }
        ).background_gradient(subset=["ratio_medio"], cmap="RdYlGn_r"),
        use_container_width=True,
        hide_index=True,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2 — SIMULADOR
# ═══════════════════════════════════════════════════════════════════════════════
with tab2:
    st.title("Simulador de Compensação")
    st.caption("Ajuste as alavancas e veja em tempo real o impacto no frete não coberto.")

    # Definição de "frete não coberto"
    # = frete de linhas onde ratio > threshold_critico, descontando o que seria aceitável
    threshold_critico = st.slider(
        "Threshold: ratio acima desse valor é considerado crítico",
        min_value=0.05, max_value=0.50, value=0.15, step=0.001, format="%.2f%%",
    )

    df_critico = df_f[df_f["ratio_frete"] > threshold_critico].copy()
    df_saudavel = df_f[df_f["ratio_frete"] <= threshold_critico].copy()

    frete_nao_coberto_base = df_critico["VALOR_FRETE"].sum() - (df_critico["VALOR_LIQUIDO_ITEM"] * threshold_critico).sum()
    frete_nao_coberto_base = max(frete_nao_coberto_base, 0)

    st.info(
        f"**Frete não coberto atual:** R$ {frete_nao_coberto_base:,.2f}  "
        f"({len(df_critico):,} linhas com ratio > {threshold_critico:.0%})"
    )

    st.divider()
    st.subheader("Alavancas de compensação")

    # ── Helper: agrega por produto ────────────────────────────────────────────
    def agg_produtos(df_in):
        cols = {
            "ocorrencias": ("VALOR_FRETE", "count"),
            "frete_total": ("VALOR_FRETE", "sum"),
            "frete_medio": ("VALOR_FRETE", "mean"),
            "valor_medio": ("VALOR_LIQUIDO_ITEM", "mean"),
            "ratio_medio": ("ratio_frete", "mean"),
        }
        return (
            df_in.groupby(["NOME_PRODUTO", "NOME_CATEGORIA"])
            .agg(**cols)
            .reset_index()
            .sort_values("frete_total", ascending=False)
        )

    # ── ALAVANCA 1 ────────────────────────────────────────────────────────────
    with st.expander("1. Ajuste de preço nos produtos com baixo ratio", expanded=True):
        pct_aumento = st.slider(
            "Aumentar preço dos produtos com ratio < threshold em:",
            min_value=0.0, max_value=0.30, value=0.05, step=0.001, format="%.2f%%",
            key="sl_preco",
        )
        receita_adicional_preco = df_saudavel["VALOR_LIQUIDO_ITEM"].sum() * pct_aumento
        st.metric("Receita adicional estimada", f"R$ {receita_adicional_preco:,.2f}")

        prod_saudavel = agg_produtos(df_saudavel)
        prod_saudavel["receita_adicional"] = prod_saudavel["valor_medio"] * prod_saudavel["ocorrencias"] * pct_aumento
        prod_saudavel["novo_preco_medio"] = prod_saudavel["valor_medio"] * (1 + pct_aumento)

        st.caption(f"{len(prod_saudavel):,} produtos com ratio ≤ {threshold_critico:.0%} que seriam reajustados")
        st.dataframe(
            prod_saudavel[["NOME_PRODUTO", "NOME_CATEGORIA", "ocorrencias", "valor_medio", "novo_preco_medio", "ratio_medio", "receita_adicional"]]
            .style.format({
                "valor_medio":       "R$ {:,.2f}",
                "novo_preco_medio":  "R$ {:,.2f}",
                "ratio_medio":       "{:.1%}",
                "receita_adicional": "R$ {:,.2f}",
            }).background_gradient(subset=["receita_adicional"], cmap="Greens"),
            use_container_width=True,
            hide_index=True,
            height=280,
        )

    # ── ALAVANCA 2 ────────────────────────────────────────────────────────────
    with st.expander("2. Valor mínimo de pedido (frete grátis acima de R$)", expanded=True):
        vmr = st.number_input(
            "Valor mínimo de pedido (R$)",
            min_value=0, max_value=5000, value=500, step=50,
        )
        linhas_acima_vmr = df_critico[df_critico["VALOR_LIQUIDO_ITEM"] >= vmr].copy()
        economia_vmr = linhas_acima_vmr["VALOR_FRETE"].sum()
        st.metric(
            "Linhas críticas que passariam a ter frete grátis",
            f"{len(linhas_acima_vmr):,}",
            f"Frete absorvido pela empresa: R$ {economia_vmr:,.2f}",
        )

        if not linhas_acima_vmr.empty:
            prod_vmr = agg_produtos(linhas_acima_vmr)
            prod_vmr["frete_absorvido"] = prod_vmr["frete_medio"] * prod_vmr["ocorrencias"]
            prod_vmr["ratio_apos_frete_gratis"] = 0.0

            st.caption("Produtos críticos que seriam beneficiados pelo valor mínimo de pedido")
            st.dataframe(
                prod_vmr[["NOME_PRODUTO", "NOME_CATEGORIA", "ocorrencias", "valor_medio", "frete_medio", "ratio_medio", "frete_absorvido"]]
                .style.format({
                    "valor_medio":     "R$ {:,.2f}",
                    "frete_medio":     "R$ {:,.2f}",
                    "ratio_medio":     "{:.1%}",
                    "frete_absorvido": "R$ {:,.2f}",
                }).background_gradient(subset=["ratio_medio"], cmap="RdYlGn_r"),
                use_container_width=True,
                hide_index=True,
                height=280,
            )
        else:
            st.info("Nenhuma linha crítica com valor acima do mínimo definido.")

    # ── ALAVANCA 3 ────────────────────────────────────────────────────────────
    with st.expander("3. Frete fixo subsidiado por categoria crítica", expanded=True):
        cats_criticas = cat_agg[cat_agg["ratio_medio"] > threshold_critico]["NOME_CATEGORIA"].tolist()
        sel_sub_cats = st.multiselect("Categorias para subsidiar", cats_criticas, default=cats_criticas[:2] if cats_criticas else [])
        frete_fixo = st.number_input("Valor fixo de frete (R$)", min_value=0, max_value=200, value=15, step=5)

        if sel_sub_cats:
            df_subsidiado = df_critico[df_critico["NOME_CATEGORIA"].isin(sel_sub_cats)].copy()
            custo_subsidio = max(df_subsidiado["VALOR_FRETE"].sum() - (frete_fixo * len(df_subsidiado)), 0)

            st.metric("Custo total do subsídio", f"R$ {custo_subsidio:,.2f}")

            prod_sub = agg_produtos(df_subsidiado)
            prod_sub["frete_fixo"] = float(frete_fixo)
            prod_sub["economia_por_venda"] = prod_sub["frete_medio"] - frete_fixo
            prod_sub["custo_subsidio_total"] = (prod_sub["frete_medio"] - frete_fixo).clip(lower=0) * prod_sub["ocorrencias"]
            prod_sub["ratio_apos_subsidio"] = frete_fixo / prod_sub["valor_medio"]

            st.caption("Produtos afetados pelo frete fixo subsidiado")
            st.dataframe(
                prod_sub[["NOME_PRODUTO", "NOME_CATEGORIA", "ocorrencias", "valor_medio", "frete_medio", "frete_fixo", "ratio_medio", "ratio_apos_subsidio", "custo_subsidio_total"]]
                .style.format({
                    "valor_medio":          "R$ {:,.2f}",
                    "frete_medio":          "R$ {:,.2f}",
                    "frete_fixo":           "R$ {:,.2f}",
                    "ratio_medio":          "{:.1%}",
                    "ratio_apos_subsidio":  "{:.1%}",
                    "custo_subsidio_total": "R$ {:,.2f}",
                })
                .background_gradient(subset=["ratio_apos_subsidio"], cmap="RdYlGn_r"),
                use_container_width=True,
                hide_index=True,
                height=280,
            )
        else:
            custo_subsidio = 0.0
            st.info("Selecione ao menos uma categoria para simular o subsídio.")

    # ── ALAVANCA 4 ────────────────────────────────────────────────────────────
    with st.expander("4. Monte seu pedido (bundle)", expanded=True):
        st.caption("Adicione produtos ao pedido, defina a quantidade de cada um e veja o ratio resultante.")

        # Todos os produtos do catálogo (df_estoque), sem filtro de estoque
        todos_produtos = (
            df_estoque
            [["PRODUTO_ID", "NOME_PRODUTO", "NOME_CATEGORIA", "PRECO_VENDA", "PESO_PRODUTO"]]
            .rename(columns={"PRECO_VENDA": "preco_venda", "PESO_PRODUTO": "peso_medio"})
            .drop_duplicates(subset="PRODUTO_ID")
            .sort_values("NOME_PRODUTO")
            .reset_index(drop=True)
        )

        # ── Seletor de transportadoras (primeiro — define quais UFs aparecem) ──
        trans_sel = st.multiselect(
            "Transportadoras para cálculo de frete",
            list(TRANSPORTADORAS.keys()),
            default=list(TRANSPORTADORAS.keys()),
            key="bundle_transportadoras",
        )

        # ── Seletor de UF — filtra só estados cobertos pelas trans. selecionadas
        ufs_cobertas = ufs_cobertas_por(trans_sel) if trans_sel else []
        ufs_disponiveis = ["Todas"] + ufs_cobertas
        uf_sel = st.selectbox(
            f"Estado de entrega ({len(ufs_cobertas)} estados cobertos pelas transportadoras selecionadas)",
            ufs_disponiveis,
            index=0,
            key="bundle_uf",
        )
        uf_destino = None if uf_sel == "Todas" else uf_sel

        # ── Seletor de produtos — por SKU ou nome ─────────────────────────────
        busca_sku = st.text_input("Buscar por SKU (PRODUTO_ID) ou nome", key="bundle_busca").strip()
        if busca_sku:
            mask_busca = (
                todos_produtos["NOME_PRODUTO"].str.contains(busca_sku, case=False, na=False)
                | todos_produtos["PRODUTO_ID"].astype(str).str.contains(busca_sku, na=False)
            )
            df_busca = todos_produtos[mask_busca]
        else:
            df_busca = todos_produtos

        lista_produtos = df_busca["NOME_PRODUTO"].tolist()

        sel_produtos = st.multiselect(
            "Selecione os produtos do pedido",
            lista_produtos,
            default=[],
            key="bundle_produtos",
        )

        if not sel_produtos:
            st.info("Selecione ao menos um produto para montar o pedido.")
        else:
            df_sel = todos_produtos[todos_produtos["NOME_PRODUTO"].isin(sel_produtos)].copy()

            # ── Quantidade por produto ────────────────────────────────────────
            st.markdown("**Quantidade por produto:**")
            qtds = {}
            cols_qtd = st.columns(min(len(df_sel), 4))
            for i, (_, row) in enumerate(df_sel.iterrows()):
                col = cols_qtd[i % len(cols_qtd)]
                qtds[row["NOME_PRODUTO"]] = col.number_input(
                    row["NOME_PRODUTO"][:40],
                    min_value=1, max_value=100, value=1, step=1,
                    key=f"qtd_{row['PRODUTO_ID']}",
                )

            # ── Cálculo do bundle ─────────────────────────────────────────────
            rows_comp = []
            valor_total_bundle = 0.0
            frete_total_bundle = 0.0

            for _, row in df_sel.iterrows():
                qtd   = qtds[row["NOME_PRODUTO"]]
                preco = row["preco_venda"] if pd.notna(row["preco_venda"]) and row["preco_venda"] > 0 else 0.0
                peso  = row["peso_medio"] if pd.notna(row["peso_medio"]) and row["peso_medio"] > 0 else 1.0

                if trans_sel:
                    frete_calc_result = calcular_frete_medio(peso, preco, trans_sel, uf_destino=uf_destino)
                    frete_unit = frete_calc_result["frete_medio"] if frete_calc_result["frete_medio"] > 0 else 0.0
                    n_trans_cobrindo = frete_calc_result.get("transportadoras_cobrindo", len(trans_sel))
                else:
                    frete_unit = 0.0
                    n_trans_cobrindo = 0

                valor_linha = preco * qtd
                frete_linha = frete_unit * qtd
                ratio_isolado = frete_unit / preco if preco > 0 else None

                valor_total_bundle += valor_linha
                frete_total_bundle += frete_linha

                rows_comp.append({
                    "Produto":            row["NOME_PRODUTO"],
                    "Categoria":          row["NOME_CATEGORIA"],
                    "Preço de venda":     preco,
                    "Frete médio unit.":  frete_unit,
                    "Qtd.":               qtd,
                    "Valor linha":        valor_linha,
                    "Frete linha":        frete_linha,
                    "ratio_isolado":      ratio_isolado,
                    "ratio_bundle":       None,
                    "Trans. cobrindo":    n_trans_cobrindo,
                })

            ratio_bundle = frete_total_bundle / valor_total_bundle if valor_total_bundle > 0 else 0
            for r in rows_comp:
                r["ratio_bundle"] = ratio_bundle

            # ── KPIs ──────────────────────────────────────────────────────────
            b1, b2, b3 = st.columns(3)
            b1.metric("Valor total do pedido", f"R$ {valor_total_bundle:,.2f}")
            b2.metric("Frete estimado do pedido", f"R$ {frete_total_bundle:,.2f}")
            b3.metric(
                "Ratio do pedido",
                f"{ratio_bundle:.2%}",
                f"{'✓ Dentro do aceitável' if ratio_bundle <= threshold_critico else '✗ Acima do threshold'}",
            )

            # ── Tabela ────────────────────────────────────────────────────────
            st.caption("Detalhamento por produto: preço atual × frete calculado × simulação")
            comp = pd.DataFrame(rows_comp)
            st.dataframe(
                comp[[
                    "Produto", "Categoria", "Qtd.",
                    "Preço de venda", "Frete médio unit.",
                    "Valor linha", "Frete linha",
                    "ratio_isolado", "ratio_bundle",
                    "Trans. cobrindo",
                ]].style.format({
                    "Preço de venda":     "R$ {:,.2f}",
                    "Frete médio unit.":  "R$ {:,.2f}",
                    "Valor linha":        "R$ {:,.2f}",
                    "Frete linha":        "R$ {:,.2f}",
                    "ratio_isolado":      "{:.2%}",
                    "ratio_bundle":       "{:.2%}",
                }).background_gradient(
                    subset=["ratio_isolado", "ratio_bundle"],
                    cmap="RdYlGn_r",
                ),
                use_container_width=True,
                hide_index=True,
            )

    st.divider()

    # Resultado consolidado da simulação
    st.subheader("Resultado da simulação")

    compensacao_total = receita_adicional_preco - custo_subsidio
    frete_residual = max(frete_nao_coberto_base - compensacao_total, 0)
    pct_compensado = min(compensacao_total / frete_nao_coberto_base * 100 if frete_nao_coberto_base > 0 else 100, 100)

    r1, r2, r3 = st.columns(3)
    r1.metric("Frete não coberto (antes)", f"R$ {frete_nao_coberto_base:,.2f}")
    r2.metric("Compensação gerada", f"R$ {compensacao_total:,.2f}", f"{pct_compensado:.1f}% coberto")
    r3.metric("Frete residual (depois)", f"R$ {frete_residual:,.2f}", f"-R$ {frete_nao_coberto_base - frete_residual:,.2f}")

    # Gráfico waterfall
    fig_wf = go.Figure(
        go.Waterfall(
            name="Compensação",
            orientation="v",
            measure=["absolute", "relative", "relative", "total"],
            x=["Frete não coberto", "Ajuste de preço", "Subsídio (custo)", "Residual"],
            y=[frete_nao_coberto_base, -receita_adicional_preco, custo_subsidio, 0],
            connector={"line": {"color": "rgb(63, 63, 63)"}},
            decreasing={"marker": {"color": "#2ecc71"}},
            increasing={"marker": {"color": "#e74c3c"}},
            totals={"marker": {"color": "#3498db"}},
        )
    )
    fig_wf.update_layout(title="Waterfall: impacto das alavancas", showlegend=False, height=350)
    st.plotly_chart(fig_wf, use_container_width=True)


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 3 — RECOMENDAÇÕES
# ═══════════════════════════════════════════════════════════════════════════════
with tab3:
    st.title("Recomendações por Produto")
    st.caption("Lista priorizada de produtos com ação sugerida baseada no perfil de ratio e volume.")

    prod_rec = (
        df_f.groupby(["PRODUTO_ID", "NOME_PRODUTO", "NOME_CATEGORIA"])
        .agg(
            ocorrencias=("VALOR_FRETE", "count"),
            frete_total=("VALOR_FRETE", "sum"),
            frete_medio=("VALOR_FRETE", "mean"),
            valor_medio=("VALOR_LIQUIDO_ITEM", "mean"),
            ratio_medio=("ratio_frete", "mean"),
            peso_medio=("PESO_CAIXA", "mean"),
        )
        .reset_index()
    )
    prod_rec = prod_rec[prod_rec["ocorrencias"] >= 3].copy()

    # Cluster simples por regras de negócio
    def classificar(row):
        if row["ratio_medio"] > 0.30:
            return "🔴 Crítico — subsidiar frete ou valor mínimo"
        elif row["ratio_medio"] > 0.15:
            if row["frete_total"] > prod_rec["frete_total"].quantile(0.75):
                return "🟠 Alto custo absoluto — negociar tabela"
            return "🟡 Ratio alto — bundle ou frete fixo"
        elif row["ratio_medio"] < 0.08 and row["valor_medio"] > 500:
            return "🟢 Âncora — produto saudável, absorve frete"
        return "⚪ Normal"

    prod_rec["acao_sugerida"] = prod_rec.apply(classificar, axis=1)
    prod_rec["impacto_prioridade"] = prod_rec["frete_total"] * prod_rec["ratio_medio"]

    prod_rec_sorted = prod_rec.sort_values("impacto_prioridade", ascending=False)

    # Filtros da tab
    col_f1, col_f2 = st.columns(2)
    filtro_acao = col_f1.multiselect(
        "Filtrar por ação",
        prod_rec_sorted["acao_sugerida"].unique().tolist(),
        default=prod_rec_sorted["acao_sugerida"].unique().tolist(),
    )
    filtro_cat_rec = col_f2.multiselect(
        "Filtrar por categoria",
        sorted(prod_rec_sorted["NOME_CATEGORIA"].unique()),
        default=sorted(prod_rec_sorted["NOME_CATEGORIA"].unique()),
    )

    df_rec_filtrado = prod_rec_sorted[
        prod_rec_sorted["acao_sugerida"].isin(filtro_acao)
        & prod_rec_sorted["NOME_CATEGORIA"].isin(filtro_cat_rec)
    ]

    st.dataframe(
        df_rec_filtrado[
            ["NOME_PRODUTO", "NOME_CATEGORIA", "ocorrencias", "frete_total",
             "frete_medio", "valor_medio", "ratio_medio", "acao_sugerida"]
        ]
        .style.format(
            {
                "frete_total": "R$ {:,.2f}",
                "frete_medio": "R$ {:,.2f}",
                "valor_medio": "R$ {:,.2f}",
                "ratio_medio": "{:.1%}",
            }
        ),
        use_container_width=True,
        hide_index=True,
        height=450,
    )

    # Download CSV
    csv = df_rec_filtrado.to_csv(index=False).encode("utf-8")
    st.download_button(
        "⬇️ Exportar recomendações (CSV)",
        data=csv,
        file_name="recomendacoes_frete.csv",
        mime="text/csv",
    )

    # Distribuição das ações
    st.subheader("Distribuição das ações sugeridas")
    fig_pie = px.pie(
        df_rec_filtrado,
        names="acao_sugerida",
        values="frete_total",
        title="Frete total por tipo de ação recomendada",
        color_discrete_sequence=px.colors.qualitative.Set2,
    )
    st.plotly_chart(fig_pie, use_container_width=True)
