import csv
import io
import json
from datetime import datetime
from dataclasses import dataclass
from typing import Dict, List

import requests

CSV_URL = (
    "https://dadosabertos.aneel.gov.br/dataset/"
    "5a583f3e-1646-4f67-bf0f-69db4203e89e/resource/"
    "fcf2906c-7c32-4b9b-a637-054e7a5234f4/download/"
    "tarifas-homologadas-distribuidoras-energia-eletrica.csv"
)

GRUPO_A_SUBGROUPS = {"A2", "A3", "A4", "AS"}
VALID_MODALIDADES = {"Azul", "Verde"}
VALID_POSTOS = {"Ponta", "Fora ponta"}
VALID_UNIDADES = {"kW", "MWh"}


def to_float_br(s: str) -> float:
    s = (s or "").strip()
    if not s:
        return 0.0
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_date(s: str):
    s = (s or "").strip()
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d").date()


def baixar_e_gerar_tarifas_front() -> List[dict]:
    resp = requests.get(CSV_URL, timeout=60)
    resp.raise_for_status()

    texto = resp.content.decode("latin1")
    arquivo = io.StringIO(texto)
    leitor = csv.DictReader(arquivo, delimiter=";")
    records = list(leitor)

    tarifas = []

    for row in records:
        subgrupo = (row.get("DscSubGrupo") or "").strip().upper()
        modalidade = (row.get("DscModalidadeTarifaria") or "").strip().capitalize()
        posto = (row.get("NomPostoTarifario") or "").strip()
        unidade = (row.get("DscUnidadeTerciaria") or "").strip()
        distribuidora = (row.get("SigAgente") or "").strip()

        if subgrupo not in GRUPO_A_SUBGROUPS:
            continue
        if modalidade not in VALID_MODALIDADES:
            continue
        if posto not in VALID_POSTOS and posto != "Não se aplica":
            continue
        if unidade not in VALID_UNIDADES:
            continue

        ini = parse_date(row.get("DatInicioVigencia"))
        fim = parse_date(row.get("DatFimVigencia"))

        vlr_te = to_float_br(row.get("VlrTE"))
        vlr_tusd = to_float_br(row.get("VlrTUSD"))

        posto_norm = posto
        if modalidade == "Verde" and unidade == "kW":
            posto_norm = "Unico"
        elif posto == "Fora ponta":
            posto_norm = "Fora de Ponta"

        if vlr_te != 0.0:
            tarifas.append(
                {
                    "distribuidora": distribuidora,
                    "data_vigencia_inicio": ini.strftime("%Y-%m-%d") if ini else None,
                    "data_vigencia_fim": fim.strftime("%Y-%m-%d") if fim else None,
                    "subgrupo": subgrupo,
                    "modalidade": modalidade,
                    "componente": "TE",
                    "posto_tarifario": posto_norm,
                    "unidade": "R$/MWh" if unidade == "MWh" else "R$/kW",
                    "valor_homologado": vlr_te,
                }
            )

        if vlr_tusd != 0.0:
            tarifas.append(
                {
                    "distribuidora": distribuidora,
                    "data_vigencia_inicio": ini.strftime("%Y-%m-%d") if ini else None,
                    "data_vigencia_fim": fim.strftime("%Y-%m-%d") if fim else None,
                    "subgrupo": subgrupo,
                    "modalidade": modalidade,
                    "componente": "TUSD",
                    "posto_tarifario": posto_norm,
                    "unidade": "R$/MWh" if unidade == "MWh" else "R$/kW",
                    "valor_homologado": vlr_tusd,
                }
            )

    with open("tarifas_front.json", "w", encoding="utf-8") as f:
        json.dump(tarifas, f, ensure_ascii=False)

    return tarifas


@dataclass
class FatoresTributarios:
    fator_icms: float
    fator_pis_cofins: float
    fator_total: float


def calcular_fatores_tributarios(aliquota_icms: float, aliquota_pis_cofins: float) -> FatoresTributarios:
    fator_icms = 1.0 / (1.0 - aliquota_icms)
    fator_pis_cofins = 1.0 / (1.0 - aliquota_pis_cofins)
    fator_total = fator_icms * fator_pis_cofins
    return FatoresTributarios(fator_icms, fator_pis_cofins, fator_total)


@dataclass
class ResultadoCativo:
    custo_demanda: float
    custo_ponta: float
    custo_fp: float
    total_cativo: float


def calcular_cativo(demanda_kw, consumo_ponta_mwh, consumo_fp_mwh,
                    tusd_demanda, tusd_cons_ponta, te_cons_ponta,
                    tusd_cons_fp, te_cons_fp, fatores: FatoresTributarios) -> ResultadoCativo:
    ft = fatores.fator_total
    custo_demanda = demanda_kw * tusd_demanda * ft
    custo_ponta = consumo_ponta_mwh * (tusd_cons_ponta + te_cons_ponta) * ft
    custo_fp = consumo_fp_mwh * (tusd_cons_fp + te_cons_fp) * ft
    total_cativo = custo_demanda + custo_ponta + custo_fp
    return ResultadoCativo(
        round(custo_demanda, 2),
        round(custo_ponta, 2),
        round(custo_fp, 2),
        round(total_cativo, 2),
    )


@dataclass
class ResultadoLivre:
    custo_tusd_demanda: float
    custo_tusd_ponta: float
    custo_tusd_fp: float
    custo_encargos: float
    custo_energia_acl: float
    total_livre: float
    saving_mensal: float


def calcular_livre_varejista(demanda_kw, consumo_ponta_mwh, consumo_fp_mwh,
                             tusd_demanda, tusd_cons_ponta, tusd_cons_fp,
                             preco_energia_acl_mwh, encargo_cde_mwh,
                             fatores: FatoresTributarios, total_cativo: float,
                             perc_desconto_tusd: float = 0.0) -> ResultadoLivre:
    ft = fatores.fator_total
    ficms = fatores.fator_icms
    consumo_total_mwh = consumo_ponta_mwh + consumo_fp_mwh

    base_demanda_cheia = demanda_kw * tusd_demanda
    imposto_demanda = (base_demanda_cheia * ft) - base_demanda_cheia
    base_demanda_com_desconto = base_demanda_cheia * (1.0 - perc_desconto_tusd)
    custo_tusd_demanda = base_demanda_com_desconto + imposto_demanda

    base_ponta_cheia = consumo_ponta_mwh * tusd_cons_ponta
    imposto_ponta = (base_ponta_cheia * ft) - base_ponta_cheia
    base_ponta_com_desconto = base_ponta_cheia * (1.0 - perc_desconto_tusd)
    custo_tusd_ponta = base_ponta_com_desconto + imposto_ponta

    base_fp_cheia = consumo_fp_mwh * tusd_cons_fp
    imposto_fp = (base_fp_cheia * ft) - base_fp_cheia
    base_fp_com_desconto = base_fp_cheia * (1.0 - perc_desconto_tusd)
    custo_tusd_fp = base_fp_com_desconto + imposto_fp

    custo_encargos = consumo_total_mwh * encargo_cde_mwh
    custo_energia_acl = consumo_total_mwh * preco_energia_acl_mwh * ficms

    total_livre = custo_tusd_demanda + custo_tusd_ponta + custo_tusd_fp + custo_encargos + custo_energia_acl
    saving_mensal = total_cativo - total_livre

    return ResultadoLivre(
        round(custo_tusd_demanda, 2),
        round(custo_tusd_ponta, 2),
        round(custo_tusd_fp, 2),
        round(custo_encargos, 2),
        round(custo_energia_acl, 2),
        round(total_livre, 2),
        round(saving_mensal, 2),
    )


def selecionar_tarifas_vigentes(tarifas: List[dict], distribuidora: str, subgrupo: str, modalidade: str) -> Dict[str, float]:
    dist_term = distribuidora.strip().upper()
    subgrupo = subgrupo.strip().upper()
    modalidade = modalidade.strip().capitalize()

    filtradas = [
        t for t in tarifas
        if t.get("distribuidora", "").strip().upper() == dist_term
        and t.get("subgrupo", "").strip().upper() == subgrupo
        and t.get("modalidade", "").strip().capitalize() == modalidade
        and t.get("data_vigencia_inicio")
    ]
    if not filtradas:
        raise ValueError("Nenhuma tarifa encontrada para esse filtro.")

    def parse_data(dstr: str) -> datetime:
        return datetime.strptime(str(dstr)[:10], "%Y-%m-%d")

    filtradas.sort(key=lambda x: parse_data(x["data_vigencia_inicio"]), reverse=True)
    data_mais_recente = filtradas[0]["data_vigencia_inicio"]
    vigentes = [t for t in filtradas if t["data_vigencia_inicio"] == data_mais_recente]

    tusd_demanda = tusd_cons_ponta = te_cons_ponta = tusd_cons_fp = te_cons_fp = None

    for t in vigentes:
        comp = t.get("componente")
        posto = t.get("posto_tarifario")
        unidade = t.get("unidade")
        valor = float(t.get("valor_homologado", 0.0))

        if comp == "TUSD" and unidade == "R$/kW":
            tusd_demanda = valor
        if unidade == "R$/MWh" and posto == "Ponta":
            if comp == "TUSD":
                tusd_cons_ponta = valor
            elif comp == "TE":
                te_cons_ponta = valor
        if unidade == "R$/MWh" and posto in ("Fora de Ponta", "Unico"):
            if comp == "TUSD":
                tusd_cons_fp = valor
            elif comp == "TE":
                te_cons_fp = valor

    return {
        "tusd_demanda": tusd_demanda,
        "tusd_cons_ponta": tusd_cons_ponta,
        "te_cons_ponta": te_cons_ponta,
        "tusd_cons_fp": tusd_cons_fp,
        "te_cons_fp": te_cons_fp,
    }


def projetar_estudo_plurianual(demanda_kw, consumo_ponta_mwh, consumo_fp_mwh,
                               tusd_demanda, tusd_cons_ponta, te_cons_ponta,
                               tusd_cons_fp, te_cons_fp, encargo_cde_mwh,
                               fatores: FatoresTributarios, perc_desconto_tusd: float,
                               cenarios_anuais: List[Dict]) -> List[Dict]:
    cativo = calcular_cativo(demanda_kw, consumo_ponta_mwh, consumo_fp_mwh,
                             tusd_demanda, tusd_cons_ponta, te_cons_ponta,
                             tusd_cons_fp, te_cons_fp, fatores)
    resultados = []
    for c in cenarios_anuais:
        livre = calcular_livre_varejista(demanda_kw, consumo_ponta_mwh, consumo_fp_mwh,
                                         tusd_demanda, tusd_cons_ponta, tusd_cons_fp,
                                         c["preco_acl"], encargo_cde_mwh,
                                         fatores, cativo.total_cativo, perc_desconto_tusd)
        resultados.append({
            "ano": c["ano"],
            "preco_acl_aplicado": c["preco_acl"],
            "total_cativo_mensal": cativo.total_cativo,
            "total_livre_mensal": livre.total_livre,
            "saving_mensal": livre.saving_mensal,
            "saving_anual_projetado": round(livre.saving_mensal * 12, 2),
        })
    return resultados


def main():
    tarifas = baixar_e_gerar_tarifas_front()

    distribuidora = "ENEL RJ"
    subgrupo = "A4"
    modalidade = "Azul"

    t = selecionar_tarifas_vigentes(tarifas, distribuidora, subgrupo, modalidade)

    demanda_kw = 650.0
    consumo_ponta_mwh = 1.05
    consumo_fp_mwh = 80.07

    aliquota_icms = 0.19
    aliquota_pis_cofins = 0.0925

    perc_desconto_tusd = 0.0  # conv
    encargo_cde_mwh = 4.22

    cenarios_anuais = [
        {"ano": 2026, "preco_acl": 295.0},
        {"ano": 2027, "preco_acl": 275.0},
        {"ano": 2028, "preco_acl": 255.0},
        {"ano": 2029, "preco_acl": 230.0},
        {"ano": 2030, "preco_acl": 205.0},
    ]

    fatores = calcular_fatores_tributarios(aliquota_icms, aliquota_pis_cofins)
    projecao = projetar_estudo_plurianual(
        demanda_kw, consumo_ponta_mwh, consumo_fp_mwh,
        t["tusd_demanda"], t["tusd_cons_ponta"], t["te_cons_ponta"],
        t["tusd_cons_fp"], t["te_cons_fp"],
        encargo_cde_mwh, fatores, perc_desconto_tusd, cenarios_anuais,
    )

    with open("projecao_estudo.json", "w", encoding="utf-8") as f:
        json.dump(projecao, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
