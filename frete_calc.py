"""
frete_calc.py — Módulo de cálculo de frete real por transportadora.

Importável standalone (sem Streamlit).
"""

from __future__ import annotations

import math
from pathlib import Path
from functools import lru_cache

import pandas as pd
import numpy as np

# ── Faixas de CEP por UF (padrão nacional) ────────────────────────────────────
# Cada UF tem um ou mais intervalos [cepi, cepf].
# Fonte: Correios / ECT — faixas oficiais de CEP por estado.
_UF_CEP_RANGES_STATIC: dict[str, list[tuple[int, int]]] = {
    "SP": [(1000001, 19999999)],
    "MS": [(79000001, 79999999)],
    "PR": [(80000000, 87999999)],
    "SC": [(88000000, 89999999)],
    "RS": [(90000000, 99999999)],
    "MG": [(30000000, 39999999)],
    "ES": [(29000000, 29999999)],
    "RJ": [(20000000, 28999999)],
    "BA": [(40000000, 48999999)],
    "SE": [(49000000, 49999999)],
    "PE": [(50000000, 56999999)],
    "AL": [(57000000, 57999999)],
    "PB": [(58000000, 58999999)],
    "RN": [(59000000, 59999999)],
    "CE": [(60000000, 63999999)],
    "PI": [(64000000, 64999999)],
    "MA": [(65000000, 65999999)],
    "PA": [(66000001, 68899999)],
    "AP": [(68900001, 68999999)],
    "AM": [(69000001, 69899999)],
    "RR": [(69300001, 69399999)],
    "AC": [(69900001, 69999999)],
    "DF": [(70000000, 73699999)],
    "GO": [(72800001, 76759999)],
    "RO": [(76800000, 76999999)],
    "TO": [(77000001, 77999999)],
    "MT": [(78000001, 78899999)],
}

# ── Caminhos dos CSVs ──────────────────────────────────────────────────────────
_BASE = Path(__file__).parent

TRANSPORTADORAS: dict[str, str] = {
    "Alfa":     str(_BASE / "Tabela fretes transportadoras - Alfa.csv"),
    "Binho":    str(_BASE / "Tabela fretes transportadoras - Binho.csv"),
    "Favorita": str(_BASE / "Tabela fretes transportadoras - Favorita.csv"),
    "Jamef":    str(_BASE / "Tabela fretes transportadoras - Jamef.csv"),
    "Loggi":    str(_BASE / "Tabela fretes transportadoras - Loggi.csv"),
    "RTE":      str(_BASE / "Tabela fretes transportadoras - RTE.csv"),
}

# ── Colunas de peso por transportadora (na ordem crescente) ───────────────────
# Favorita usa kg direto (5, 10, ...). Demais usam milhar (10.000 = 10 kg).
_WEIGHT_COLS: dict[str, list[str]] = {
    "Alfa":     ["10.000", "20.000", "40.000", "60.000", "70.000", "100.000"],
    "Binho":    ["10.000", "20.000", "30.000", "50.000", "75.000", "100.000"],
    "Favorita": ["5", "10", "20", "30", "40", "50", "60", "70", "100", "150", "200"],
    "Jamef":    ["10.000", "20.000", "30.000", "50.000", "75.000", "100.000"],
    "Loggi":    ["0.300", "0.500", "0.750", "1.000", "1.250", "1.500", "2.000",
                 "2.500", "3.000", "3.500", "4.000", "5.000", "6.000", "7.000",
                 "8.000", "9.000", "10.000", "11.000", "12.000", "13.000",
                 "14.000", "15.000", "20.000", "25.000", "30.000"],
    "RTE":      ["5.000", "10.000", "15.000", "20.000", "30.000", "40.000",
                 "50.000", "60.000", "70.000", "80.000", "90.000", "100.000"],
}

# Favorita — colunas em kg direto (sem milhar)
_FAVORITA_DIRECT_KG = True


def _col_to_kg(col_name: str, favorita: bool = False) -> float:
    """Converte nome de coluna de peso para kg (float)."""
    if favorita:
        return float(col_name.replace(",", "."))
    # Remove separador de milhar e converte — "10.000" → 10.0
    return float(col_name.replace(".", "")) / 1000.0 if "." in col_name else float(col_name)


def _parse_num(val) -> float:
    """Converte string com vírgula decimal para float. NaN/vazio → 0.0."""
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return 0.0
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", ""):
        return 0.0
    # Remove % se presente
    s = s.replace("%", "")
    # Troca vírgula por ponto
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_pct_str(val) -> float:
    """
    Converte string de porcentagem para fração decimal.
    "0,60%" → 0.006   |   "0.15" → 0.0015 (já em formato %)   |   0.15 → 0.0015
    Se o valor bruto >= 1, assume que está em % e divide por 100.
    """
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return 0.0
    s = str(val).strip().replace("%", "").replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return 0.0
    # Valores de % nas tabelas: 0.15 significa 0.15%, 0.60% → 0.60%
    # Para passar a fração decimal: dividir por 100
    return v / 100.0


@lru_cache(maxsize=None)
def _load_csv(nome: str) -> pd.DataFrame:
    """Carrega e cacheia o CSV de uma transportadora (strings brutas)."""
    path = TRANSPORTADORAS[nome]
    return pd.read_csv(path, dtype=str)


@lru_cache(maxsize=1)
def get_uf_ranges() -> dict[str, tuple[int, int]]:
    """
    Retorna {UF: (cepi_min, cepf_max)} baseado na tabela Favorita + faixas nacionais.

    Utiliza a Favorita (que tem coluna UF) para refinar os ranges das UFs que ela
    cobre; para as demais UFs, usa as faixas padrão nacionais dos Correios.
    """
    # Começa com as faixas estáticas nacionais (min de cada UF)
    result: dict[str, tuple[int, int]] = {
        uf: (min(r[0] for r in ranges), max(r[1] for r in ranges))
        for uf, ranges in _UF_CEP_RANGES_STATIC.items()
    }

    # Refina com dados reais da Favorita (que já tem coluna UF)
    try:
        df_fav = _load_csv("Favorita").copy()
        if "UF" in df_fav.columns and "CEPI" in df_fav.columns and "CEPF" in df_fav.columns:
            df_fav["CEPI"] = pd.to_numeric(df_fav["CEPI"], errors="coerce")
            df_fav["CEPF"] = pd.to_numeric(df_fav["CEPF"], errors="coerce")
            df_fav = df_fav.dropna(subset=["CEPI", "CEPF", "UF"])
            grouped = (
                df_fav.groupby("UF")
                .agg(cepi_min=("CEPI", "min"), cepf_max=("CEPF", "max"))
                .reset_index()
            )
            for _, row in grouped.iterrows():
                uf = str(row["UF"]).strip().upper()
                cepi = int(row["cepi_min"])
                cepf = int(row["cepf_max"])
                # Favorita tem precedência para as UFs que ela cobre
                result[uf] = (cepi, cepf)
    except Exception:
        pass  # Fallback silencioso para faixas estáticas

    return result


# Cobertura pré-calculada: quais UFs cada transportadora atende
COBERTURA_UF: dict[str, list[str]] = {
    "Alfa":     ["DF", "ES", "GO", "MG", "PR", "RJ", "RS", "SC", "SP"],
    "Binho":    ["DF", "ES", "GO", "PA", "RJ", "SP"],
    "Favorita": ["AC", "AM", "AP", "DF", "GO", "MS", "MT", "PA", "RO", "RR", "TO"],
    "Jamef":    ["SP"],
    "Loggi":    ["AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG",
                 "MS", "MT", "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR",
                 "RS", "SC", "SE", "SP", "TO"],
    "RTE":      ["AL", "AM", "AP", "DF", "ES", "GO", "MG", "MS", "MT", "PA", "PR",
                 "RJ", "RN", "RR", "RS", "SC", "SP"],
}


def ufs_cobertas_por(transportadoras_sel: list[str]) -> list[str]:
    """Retorna a união dos estados cobertos pelas transportadoras selecionadas, ordenada."""
    ufs = set()
    for t in transportadoras_sel:
        ufs.update(COBERTURA_UF.get(t, []))
    return sorted(ufs)


def _get_weight_cols_for(nome: str, df: pd.DataFrame) -> list[str]:
    """Retorna apenas as colunas de peso que existem no DataFrame."""
    return [c for c in _WEIGHT_COLS[nome] if c in df.columns]


def _get_frete_base(
    peso_kg: float,
    row: pd.Series,
    weight_cols: list[str],
    is_favorita: bool = False,
    valor_excedente: float = 0.0,
) -> float:
    """
    Calcula frete_base para uma linha de tarifa dado o peso.

    - Encontra a menor coluna de peso >= peso_kg (ignorando zeros).
    - Se peso > maior coluna disponível: usa VALOR EXCEDENTE.
    """
    # Converte colunas disponíveis para (kg, valor)
    cols_kg = [(c, _col_to_kg(c, is_favorita)) for c in weight_cols]

    # Remove colunas com valor zero (tarifa não aplicável)
    valid: list[tuple[str, float, float]] = []
    for col, kg_limit in cols_kg:
        v = _parse_num(row.get(col, "0"))
        if v > 0:
            valid.append((col, kg_limit, v))

    if not valid:
        return 0.0

    max_kg = valid[-1][1]
    max_val = valid[-1][2]

    if peso_kg > max_kg:
        # Usa valor excedente
        if valor_excedente > 0:
            return max_val + (peso_kg - max_kg) * valor_excedente
        else:
            return max_val

    # Encontra a menor faixa >= peso_kg
    for _, kg_limit, val in valid:
        if kg_limit >= peso_kg:
            return val

    # Fallback: maior disponível
    return max_val


def _calc_pedagio(peso_kg: float, row: pd.Series) -> float:
    """Pedágio: valor_fixo + ceil(peso / fracao_kg) * valor_fixo."""
    fixo = _parse_num(row.get("PEDÁGIO VALOR FIXO", 0))
    fracao = _parse_num(row.get("PEDÁGIO FRAÇÃO A CADA x KG", 0))
    if fixo == 0:
        return 0.0
    if fracao <= 0:
        return fixo
    # valor_fixo é o custo por fração de peso
    return math.ceil(peso_kg / fracao) * fixo


def _calc_frete_completo(
    nome: str,
    peso_kg: float,
    valor_nf: float,
    row: pd.Series,
    weight_cols: list[str],
) -> float:
    """Calcula o frete completo para uma linha de tarifa (região)."""

    is_favorita = nome == "Favorita"

    valor_excedente = _parse_num(row.get("VALOR EXCEDENTE", 0))
    frete_base = _get_frete_base(peso_kg, row, weight_cols, is_favorita, valor_excedente)

    # ── frete sobre nota ──────────────────────────────────────────────────────
    frete_nota_pct = _parse_pct_str(row.get("FRETE VALOR SOBRE A NOTA(%)", 0))
    frete_nota = valor_nf * frete_nota_pct if frete_nota_pct > 0 else 0.0

    frete_total_base = frete_base + frete_nota

    # ── GRIS ──────────────────────────────────────────────────────────────────
    gris = 0.0
    gris_pct_raw = row.get("GRIS(%)", None)
    if gris_pct_raw is not None and str(gris_pct_raw).strip() not in ("", "nan", "0", "0.0"):
        gris_pct = _parse_pct_str(gris_pct_raw)
        gris_raw = valor_nf * gris_pct
        gris_min = _parse_num(row.get("GRIS MÍNIMO", 0))
        gris = max(gris_raw, gris_min)
        gris_max_raw = row.get("GRIS MÁXIMO", None)
        if gris_max_raw is not None:
            gris_max = _parse_num(gris_max_raw)
            if gris_max > 0:
                gris = min(gris, gris_max)

    # ── Pedágio ───────────────────────────────────────────────────────────────
    pedagio = _calc_pedagio(peso_kg, row)

    # ── TAS ───────────────────────────────────────────────────────────────────
    tas = _parse_num(row.get("TAS VALOR FIXO", 0))

    # ── Entrega ───────────────────────────────────────────────────────────────
    entrega = _parse_num(row.get("ENTREGA VALOR FIXO", 0))

    # ── Seguro (RTE) ──────────────────────────────────────────────────────────
    seguro = 0.0
    seguro_pct_raw = row.get("SEGURO(%)", None)
    if seguro_pct_raw is not None and _parse_num(seguro_pct_raw) > 0:
        seguro_pct = _parse_pct_str(seguro_pct_raw)
        seguro_min = _parse_num(row.get("SEGURO MÍNIMO", 0))
        seguro = max(valor_nf * seguro_pct, seguro_min)

    # ── SECCAT (RTE) ──────────────────────────────────────────────────────────
    seccat = _parse_num(row.get("SECCAT VALOR FIXO", 0))

    # ── Total ─────────────────────────────────────────────────────────────────
    frete_final = (
        frete_total_base
        + gris
        + pedagio
        + tas
        + entrega
        + seguro
        + seccat
    )

    return frete_final


def _fretes_por_transportadora(
    nome: str,
    peso_kg: float,
    valor_nf: float,
    uf_destino: str | None = None,
) -> list[float]:
    """
    Calcula o frete para cada região única da transportadora.
    Retorna lista de fretes (um por região).

    Se uf_destino for fornecido, filtra apenas as linhas cujo range [CEPI, CEPF]
    intersecta com o range de CEP da UF informada.
    """
    df = _load_csv(nome).copy()
    weight_cols = _get_weight_cols_for(nome, df)

    if not weight_cols:
        return []

    # ── Filtro por UF ─────────────────────────────────────────────────────────
    if uf_destino is not None:
        uf_upper = uf_destino.strip().upper()

        if nome == "Favorita" and "UF" in df.columns:
            # Favorita tem coluna UF direta — filtra por ela
            df = df[df["UF"].str.strip().str.upper() == uf_upper]
            if df.empty:
                return []
        else:
            uf_ranges = get_uf_ranges()
            if uf_upper in uf_ranges and "CEPI" in df.columns and "CEPF" in df.columns:
                uf_cepi_min, uf_cepf_max = uf_ranges[uf_upper]

                df["_cepi_num"] = pd.to_numeric(df["CEPI"], errors="coerce")
                df["_cepf_num"] = pd.to_numeric(df["CEPF"], errors="coerce")

                mask = (
                    (df["_cepi_num"] <= uf_cepf_max) &
                    (df["_cepf_num"] >= uf_cepi_min)
                )
                df = df[mask]
                df = df.drop(columns=["_cepi_num", "_cepf_num"])

                if df.empty:
                    return []

    # Colunas que definem uma "região" = combinação única de todos os campos de tarifa
    # (exclui CEPI, CEPF, PRAZO e campos de faixa-inativa que não afetam o cálculo)
    skip_cols = {
        "CEPI", "CEPF", "PRAZO(DIAS ÚTEIS)", "UF", "REGIÃO",
        "FAIXA INICIAL DE GRIS",
        "FAIXA INICIAL DE COLETA",
        "FAIXA FINAL DE COLETA",
        "FAIXA INICIAL DE CTE",
        "FAIXA FINAL DE CTE",
        "_cepi_num", "_cepf_num",
    }
    tariff_cols = [c for c in df.columns if c not in skip_cols]

    # Desduplicar regiões
    regioes = df[tariff_cols].drop_duplicates()

    fretes: list[float] = []

    for _, row in regioes.iterrows():
        f = _calc_frete_completo(nome, peso_kg, valor_nf, row, weight_cols)
        fretes.append(f)

    return fretes


def calcular_frete_medio(
    peso_kg: float,
    valor_nf: float,
    transportadoras_sel: list[str],
    uf_destino: str | None = None,
) -> dict:
    """
    Calcula o frete médio estimado para um produto.

    Parâmetros
    ----------
    peso_kg : float
        Peso do produto em kg.
    valor_nf : float
        Valor da nota fiscal (PRECO_VENDA ou VALOR_LIQUIDO_ITEM).
    transportadoras_sel : list[str]
        Nomes das transportadoras a considerar (subconjunto de TRANSPORTADORAS).
    uf_destino : str | None
        UF de destino (ex: "SP", "MG"). None = considera todas as regiões.
        Quando informado, filtra as linhas do CSV onde o range [CEPI, CEPF]
        intersecta com o range de CEP da UF, excluindo regiões remotas.

    Retorna
    -------
    dict com:
        'frete_medio'        — média de todos os fretes de todas as regiões/transportadoras
        'por_transportadora' — {nome: {'frete_medio': float, 'n_regioes': int}}
        'n_regioes'          — total de regiões consideradas
        'transportadoras_cobrindo' — número de transportadoras com cobertura na UF
    """
    if peso_kg <= 0 or valor_nf <= 0:
        return {
            "frete_medio": 0.0,
            "por_transportadora": {},
            "n_regioes": 0,
            "transportadoras_cobrindo": 0,
        }

    todos_fretes: list[float] = []
    por_trans: dict[str, dict] = {}
    n_regioes = 0
    transportadoras_cobrindo = 0

    for nome in transportadoras_sel:
        if nome not in TRANSPORTADORAS:
            continue
        fretes = _fretes_por_transportadora(nome, peso_kg, valor_nf, uf_destino)
        # Filtra zeros (regiões sem tarifa válida)
        fretes_validos = [f for f in fretes if f > 0]
        if fretes_validos:
            media_trans = float(np.mean(fretes_validos))
            por_trans[nome] = {
                "frete_medio": round(media_trans, 2),
                "n_regioes": len(fretes_validos),
            }
            todos_fretes.extend(fretes_validos)
            n_regioes += len(fretes_validos)
            transportadoras_cobrindo += 1
        else:
            por_trans[nome] = {"frete_medio": 0.0, "n_regioes": 0}

    frete_medio = round(float(np.mean(todos_fretes)), 2) if todos_fretes else 0.0

    return {
        "frete_medio": frete_medio,
        "por_transportadora": por_trans,
        "n_regioes": n_regioes,
        "transportadoras_cobrindo": transportadoras_cobrindo,
    }
