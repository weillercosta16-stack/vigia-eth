"""Modo Cripto: vigia o ETH/USDT da Binance com as regras do Weiller e avisa quando chega a hora da entrada.

Regras (definidas por ele):
  * O gráfico de 1 DIA define a tendência: preço acima ou abaixo da EMA 200, e topos/fundos subindo ou descendo.
  * O gráfico de 1 HORA dá o ponto de entrada pelo RSI 14 (candle fechado):
      - tendência de alta (acima da EMA 200): RSI abaixo de 40 = entrada; acima de 66 = aguardar
      - tendência de baixa (abaixo da EMA 200): RSI entre 20 e 35 = entrada
  * Só avisa a entrada se algum alvo der pelo menos 3 por 1: lucro líquido >= 3x a perda do stop (com a taxa).
  * Reforços (entrada forte): Bollinger de baixo, suporte por perto (fundos anteriores, EMA 21/50), tendência confirmada.
  * Cuidados: resistência logo acima, preço furando a EMA 50 para baixo.

Nunca compra nem vende nada: só calcula e avisa. A janela fica minimizada e abre sozinha no monitor 2 quando há aviso.
Uso: pythonw cripto.py            (o Jarvis abre sozinho: "Jarvis, modo cripto")
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

PASTA = Path(__file__).resolve().parent
logging.basicConfig(filename=PASTA / "cripto.log", level=logging.INFO, encoding="utf-8",
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cripto")

AJUSTES = PASTA / "cripto.json"            # canal do celular, histórico de avisos (não mexe no config do Jarvis)
ESTADO = PASTA / "cripto_estado.json"      # última leitura, para o Jarvis responder "como está o ETH?"
CMD = PASTA / "cripto_cmd.txt"             # o Jarvis escreve "mostrar" ou "sair" aqui
FALA = PASTA / "cripto_falar.txt"          # o que o Jarvis deve falar em voz alta
TITULO = "Jarvis · Modo Cripto"

PADRAO = {
    "par": "ETHUSDT",
    "monitor_alerta": 2,
    "rsi_alta_entrada": 40,        # acima da EMA 200: RSI abaixo disto = entrada
    "rsi_alta_aguardar": 66,       # acima da EMA 200: RSI acima disto = aguardar
    "rsi_baixa_entrada": 35,       # abaixo da EMA 200: RSI entre o mínimo e isto = entrada
    "rsi_baixa_min": 20,
    "rsi_saida_diario": 66,        # RSI do DIÁRIO acima disto = sair (realizar o lucro)
    "valor_trade": 10000,          # US$ por operação (muda na tela)
    "taxa_pct": 1.5,               # taxa total (entrada + saída) em % do valor: 10 mil → 150
    "margem_alvo": 0.3,            # % — sai um pouco antes de tocar na resistência
    "rr_minimo": 3,                # só avisa entrada se o lucro líquido for pelo menos 3x a perda do stop
    "voz": True,                   # o Jarvis fala o sinal
    "margem_ema200": 0.3,          # % — colado na EMA 200 = indefinido
    "perto_ema": 0.5,              # % — "testando a EMA"
    "perto_nivel": 1.0,            # % — suporte/resistência "por perto"
    "zona_juntar": 0.5,            # % — topos/fundos a menos disto viram a mesma zona
    "quase_la": 4,                 # pontos de RSI antes da zona = "quase lá"
    "intervalo": 10,               # segundos entre leituras
    "ntfy_topico": "",
    "ntfy_servidor": "https://ntfy.sh",
    "celular": True,
    "bipe": True,
}

BASES = ("https://api.binance.com", "https://data-api.binance.vision", "https://api.binance.us")


def ler_ajustes() -> dict:
    a = dict(PADRAO)
    try:
        a.update(json.loads(AJUSTES.read_text(encoding="utf-8")))
    except Exception:
        pass
    if not a.get("ntfy_topico"):
        a["ntfy_topico"] = "jarvis-cripto-" + secrets.token_hex(5)
        salvar_ajustes(a)
    return a


_TRAVA_ARQ = threading.RLock()          # salvar_ajustes e gravar_desenhos mexem no mesmo arquivo
_DESENHOS_VISTOS = {"v": None}          # último "desenhos" lido do arquivo (reserva se ele estiver sendo gravado)


def salvar_ajustes(a: dict):
    with _TRAVA_ARQ:
        _salvar_ajustes(a)


def _salvar_ajustes(a: dict):
    try:
        dados = {k: v for k, v in a.items() if k != "desenhos"}
        # os desenhos do gráfico ficam como estão no arquivo (os agentes escrevem direto lá)
        for tentativa in range(3):
            try:
                no_disco = json.loads(AJUSTES.read_text(encoding="utf-8"))
                if isinstance(no_disco, dict) and isinstance(no_disco.get("desenhos"), list):
                    _DESENHOS_VISTOS["v"] = no_disco["desenhos"]
                    dados["desenhos"] = no_disco["desenhos"]
                break
            except FileNotFoundError:
                break
            except Exception:          # arquivo sendo gravado por outro: tenta de novo e, no fim, usa o último visto
                if tentativa == 2:
                    if _DESENHOS_VISTOS["v"] is not None:
                        dados["desenhos"] = _DESENHOS_VISTOS["v"]
                else:
                    time.sleep(0.15)
        tmp = AJUSTES.with_suffix(".tmp")
        tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, AJUSTES)
    except Exception as e:
        log.warning("salvar ajustes: %s", e)


# ------------------------------------------------------------------ dados da Binance
_base_boa = {"url": None}


def baixar_json(caminho: str, params: dict):
    ordem = ([_base_boa["url"]] if _base_boa["url"] else []) + [b for b in BASES if b != _base_boa["url"]]
    erro = None
    for base in ordem:
        try:
            url = f"{base}{caminho}?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url, headers={"User-Agent": "Jarvis/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                dados = json.loads(r.read().decode())
            if _base_boa["url"] != base:
                log.info("dados vindo de %s", base)
            _base_boa["url"] = base
            return dados
        except Exception as e:
            erro = e
    raise RuntimeError(f"Binance não respondeu: {erro}")


def candles(par: str, tempo: str, n: int = 500) -> list[dict]:
    brutos = baixar_json("/api/v3/klines", {"symbol": par, "interval": tempo, "limit": n})
    return [{"t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]),
             "fim": int(k[6])} for k in brutos]


# ------------------------------------------------------------------ indicadores (iguais aos da Binance / TradingView)
def ema(valores: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(valores)
    if len(valores) < n:
        return out
    k = 2 / (n + 1)
    atual = sum(valores[:n]) / n
    out[n - 1] = atual
    for i in range(n, len(valores)):
        atual = valores[i] * k + atual * (1 - k)
        out[i] = atual
    return out


def rsi(valores: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(valores)
    if len(valores) <= n:
        return out
    ganhos = [max(valores[i] - valores[i - 1], 0) for i in range(1, len(valores))]
    perdas = [max(valores[i - 1] - valores[i], 0) for i in range(1, len(valores))]
    mg, mp = sum(ganhos[:n]) / n, sum(perdas[:n]) / n
    def calc(g, p):
        return 100.0 if p == 0 else 100 - 100 / (1 + g / p)
    out[n] = calc(mg, mp)
    for i in range(n, len(ganhos)):
        mg = (mg * (n - 1) + ganhos[i]) / n
        mp = (mp * (n - 1) + perdas[i]) / n
        out[i + 1] = calc(mg, mp)
    return out


def bollinger(valores: list[float], n: int = 20, k: float = 2.0):
    meio, cima, baixo = [None] * len(valores), [None] * len(valores), [None] * len(valores)
    for i in range(n - 1, len(valores)):
        janela = valores[i - n + 1: i + 1]
        m = sum(janela) / n
        dp = (sum((x - m) ** 2 for x in janela) / n) ** 0.5
        meio[i], cima[i], baixo[i] = m, m + k * dp, m - k * dp
    return meio, cima, baixo


def pivos(cs: list[dict], lado: int = 2):
    """Topos e fundos: máxima (ou mínima) maior (menor) que os 'lado' candles de cada lado. Só candles já fechados."""
    topos, fundos = [], []
    for i in range(lado, len(cs) - lado):
        h, l = cs[i]["h"], cs[i]["l"]
        viz = cs[i - lado: i] + cs[i + 1: i + 1 + lado]
        if all(h > v["h"] for v in viz):
            topos.append((i, h))
        if all(l < v["l"] for v in viz):
            fundos.append((i, l))
    return topos, fundos


def estrutura(topos, fundos) -> str:
    if len(topos) < 2 or len(fundos) < 2:
        return "indefinida"
    t1, t2 = topos[-2][1], topos[-1][1]
    f1, f2 = fundos[-2][1], fundos[-1][1]
    if t2 > t1 and f2 > f1:
        return "alta"
    if t2 < t1 and f2 < f1:
        return "baixa"
    return "lateral"


def zonas(niveis: list[tuple[float, str]], juntar_pct: float) -> list[dict]:
    """Junta topos/fundos de preço parecido numa zona. Retorna [{preco, toques, origem}]."""
    out: list[dict] = []
    for preco, origem in sorted(niveis):
        if out and abs(preco - out[-1]["preco"]) / out[-1]["preco"] * 100 <= juntar_pct:
            z = out[-1]
            z["soma"] += preco; z["toques"] += 1; z["origem"].add(origem)
            z["preco"] = z["soma"] / z["toques"]
        else:
            out.append({"preco": preco, "soma": preco, "toques": 1, "origem": {origem}})
    return [{"preco": z["preco"], "toques": z["toques"], "origem": "+".join(sorted(z["origem"]))} for z in out]


def r2(v):
    return None if v is None else round(v, 2)


def serie_grafico(cs: list[dict], lado: int, n: int = 500) -> dict:
    """Velas + indicadores prontos para o gráfico (tempo em segundos UTC)."""
    c = [x["c"] for x in cs]
    e21, e50, e200 = ema(c, 21), ema(c, 50), ema(c, 200)
    bm, bc, bb = bollinger(c, 20, 2)
    rs = rsi(c, 14)
    tp, fd = pivos(cs[:-1], lado)
    ini = max(0, len(cs) - n)
    corta = lambda arr: [r2(v) for v in arr[ini:]]
    return {
        "t": [x["t"] // 1000 for x in cs[ini:]],
        "o": corta([x["o"] for x in cs]), "h": corta([x["h"] for x in cs]),
        "l": corta([x["l"] for x in cs]), "c": corta(c),
        "e21": corta(e21), "e50": corta(e50), "e200": corta(e200),
        "bc": corta(bc), "bm": corta(bm), "bb": corta(bb), "rsi": corta(rs),
        "topos": [[cs[i]["t"] // 1000, r2(p)] for i, p in tp if i >= ini],
        "fundos": [[cs[i]["t"] // 1000, r2(p)] for i, p in fd if i >= ini],
    }


def pct(a: float, b: float) -> float:
    return (a - b) / b * 100


# ------------------------------------------------------------------ análise com as regras dele
def analisar(d1: list[dict], h1: list[dict], aj: dict) -> dict:
    preco = h1[-1]["c"]

    # --- diário: tendência
    c1 = [c["c"] for c in d1]
    e200d = ema(c1, 200)[-1]
    e21d, e50d = ema(c1, 21)[-1], ema(c1, 50)[-1]
    rsi_d = rsi(c1, 14)[-1]
    fech_d = d1[:-1]                                           # sem o candle de hoje (ainda aberto)
    td, fd = pivos(fech_d, 2)
    estr = estrutura(td, fd)
    if e200d is None:
        lado = "indefinido"
        dist200 = None
    else:
        dist200 = pct(preco, e200d)
        lado = "alta" if dist200 > aj["margem_ema200"] else "baixa" if dist200 < -aj["margem_ema200"] else "indefinido"
    if lado != "indefinido" and estr == lado:
        forca = "forte"
    elif lado != "indefinido" and estr in ("alta", "baixa") and estr != lado:
        forca = "em dúvida"
    else:
        forca = "normal"

    # --- 1 hora: ponto de entrada (candle fechado = h1[-2])
    c = [x["c"] for x in h1]
    r = rsi(c, 14)
    rsi_vivo, rsi_fechado = r[-1], r[-2]
    e21, e50, e200h = ema(c, 21), ema(c, 50), ema(c, 200)
    bm, bc, bb = bollinger(c, 20, 2)
    fechado = h1[-2]

    def rel_ema(nome, serie):
        v = serie[-1]
        if v is None:
            return None
        papel = "suporte" if preco >= v else "resistência"
        ant, agora = serie[-3], serie[-2]
        evento = ""
        if ant is not None and agora is not None:
            if h1[-3]["c"] < ant and h1[-2]["c"] > agora:
                evento = "rompeu pra cima"
            elif h1[-3]["c"] > ant and h1[-2]["c"] < agora:
                evento = "furou pra baixo"
        dist = pct(preco, v)
        return {"nome": nome, "valor": v, "papel": papel, "dist": dist,
                "testando": abs(dist) <= aj["perto_ema"], "evento": evento}

    emas_h = [x for x in (rel_ema("EMA 21", e21), rel_ema("EMA 50", e50)) if x]

    # posição na Bollinger (0 = banda de baixo, 1 = banda de cima)
    pos_bb = None
    if bb[-1] is not None and bc[-1] != bb[-1]:
        pos_bb = (preco - bb[-1]) / (bc[-1] - bb[-1])
    toque_baixo = bb[-2] is not None and (fechado["l"] <= bb[-2] or (pos_bb is not None and pos_bb <= 0.08))
    toque_cima = bc[-2] is not None and (fechado["h"] >= bc[-2] or (pos_bb is not None and pos_bb >= 0.92))

    # --- suportes e resistências: topos/fundos anteriores (diário + 1h)
    th, fh = pivos(h1[:-1], 3)
    niveis = [(p, "fundo 1D") for _, p in fd[-12:]] + [(p, "topo 1D") for _, p in td[-12:]] \
        + [(p, "fundo 1h") for _, p in fh[-20:]] + [(p, "topo 1h") for _, p in th[-20:]]
    zs = zonas(niveis, aj["zona_juntar"])
    abaixo = sorted([z for z in zs if z["preco"] < preco], key=lambda z: -z["preco"])[:3]
    acima = sorted([z for z in zs if z["preco"] > preco], key=lambda z: z["preco"])[:8]
    for z in abaixo + acima:
        z["dist"] = pct(z["preco"], preco)

    # --- regra principal (RSI no candle fechado de 1h, conforme a tendência do diário)
    regra_lado = lado if lado != "indefinido" else "baixa"     # colado na EMA 200: usa a regra mais exigente
    limite = aj["rsi_alta_entrada"] if regra_lado == "alta" else aj["rsi_baixa_entrada"]
    minimo = aj["rsi_baixa_min"] if regra_lado == "baixa" else 0
    na_zona = rsi_fechado is not None and minimo <= rsi_fechado < limite
    na_zona_vivo = rsi_vivo is not None and minimo <= rsi_vivo < limite
    sair = rsi_d is not None and rsi_d > aj["rsi_saida_diario"]
    aguardar = regra_lado == "alta" and rsi_fechado is not None and rsi_fechado > aj["rsi_alta_aguardar"]

    reforcos, cuidados = [], []
    if toque_baixo:
        reforcos.append("preço na banda de baixo da Bollinger")
    sup_perto = next((z for z in abaixo if abs(z["dist"]) <= aj["perto_nivel"]), None)
    if sup_perto:
        reforcos.append(f"suporte de {sup_perto['origem']} em {sup_perto['preco']:,.2f} ({sup_perto['toques']} toque(s))")
    for e in emas_h:
        if e["papel"] == "suporte" and e["testando"]:
            reforcos.append(f"{e['nome']} servindo de suporte")
        if e["evento"] == "rompeu pra cima":
            reforcos.append(f"rompeu a {e['nome']} pra cima")
    if forca == "forte":
        reforcos.append(f"tendência de {lado} confirmada no diário (EMA 200 + topos e fundos)")
    res_perto = next((z for z in acima if abs(z["dist"]) <= aj["perto_nivel"]), None)
    if res_perto:
        cuidados.append(f"resistência logo acima em {res_perto['preco']:,.2f} ({res_perto['dist']:+.2f}%)")
    for e in emas_h:
        if e["papel"] == "resistência" and e["testando"]:
            cuidados.append(f"{e['nome']} fazendo resistência logo acima")
        if e["evento"] == "furou pra baixo" and e["nome"] == "EMA 50":
            cuidados.append("acabou de furar a EMA 50 pra baixo")
    if forca == "em dúvida":
        cuidados.append("EMA 200 e topos/fundos do diário discordam (tendência em dúvida)")
    if lado == "indefinido":
        cuidados.append("preço colado na EMA 200 do diário")
    if toque_cima:
        cuidados.append("preço na banda de cima da Bollinger")

    if sair:
        nivel = "sair"
    elif na_zona:
        nivel = "forte" if len(reforcos) >= 2 else "entrada"
    elif aguardar:
        nivel = "aguardar"
    elif na_zona_vivo or (rsi_fechado is not None and rsi_fechado < limite + aj["quase_la"]):
        nivel = "quase"
    else:
        nivel = "neutro"

    tend_txt = {"alta": "ALTA (acima da EMA 200)", "baixa": "BAIXA (abaixo da EMA 200)",
                "indefinido": "colado na EMA 200"}[lado]
    return {
        "hora": time.strftime("%H:%M:%S"), "par": aj["par"], "preco": preco,
        "candle_1h": fechado["t"], "proximo_fechamento": h1[-1]["fim"],
        "diario": {"lado": lado, "texto": tend_txt, "ema200": e200d, "dist200": dist200, "estrutura": estr,
                   "forca": forca, "ema21": e21d, "ema50": e50d, "rsi": rsi_d, "rsi_saida": aj["rsi_saida_diario"],
                   "topos": [p for _, p in td[-3:]], "fundos": [p for _, p in fd[-3:]]},
        "h1": {"rsi_fechado": rsi_fechado, "rsi_vivo": rsi_vivo, "limite": limite, "minimo": minimo, "regra": regra_lado,
               "aguardar_acima": aj["rsi_alta_aguardar"],
               "boll": {"cima": bc[-1], "meio": bm[-1], "baixo": bb[-1], "pos": pos_bb},
               "emas": emas_h, "ema200": e200h[-1]},
        "suportes": abaixo, "resistencias": acima,
        "nivel": nivel, "reforcos": reforcos, "cuidados": cuidados,
        "grafico": {"1h": serie_grafico(h1, 3), "1d": serie_grafico(d1, 2)},
    }


def plano(a: dict, aj: dict) -> dict:
    """Alvos de saída antes das resistências (topos anteriores), com lucro líquido da taxa, e o stop no suporte."""
    preco, valor, taxa_pct, m = a["preco"], float(aj["valor_trade"]), float(aj["taxa_pct"]), float(aj["margem_alvo"])
    taxa = valor * taxa_pct / 100
    stop = None
    if a["suportes"]:
        z = a["suportes"][0]
        p = z["preco"] * (1 - m / 100)
        stop = {"preco": p, "suporte": z["preco"], "perda": valor * (p - preco) / preco - taxa, "pct": (p - preco) / preco * 100}
    risco = abs(stop["perda"]) if stop else None
    alvos = []
    for z in a["resistencias"]:
        alvo = z["preco"] * (1 - m / 100)
        liquido = valor * (alvo - preco) / preco - taxa
        alvos.append({"preco": alvo, "resistencia": z["preco"], "origem": z["origem"], "liquido": liquido,
                      "pct": (alvo - preco) / preco * 100, "rr": liquido / risco if risco else None})
    bons = [x for x in alvos if x["liquido"] > 0][:3]
    # regra do Weiller: só entra se o lucro líquido for pelo menos rr_minimo vezes a perda do stop (3 por 1)
    rr_min = float(aj.get("rr_minimo", 3))
    vale = [x for x in alvos if x["rr"] is not None and x["rr"] >= rr_min]
    return {"valor": valor, "taxa": taxa, "eth": valor / preco, "empate": preco * (1 + taxa_pct / 100),
            "alvos": alvos, "bons": bons, "stop": stop, "rr_min": rr_min, "melhor": vale[0] if vale else None}


def numero_falado(v: float) -> str:
    return f"{v:,.0f}".replace(",", ".")


def texto_aviso(a: dict, aj: dict | None = None) -> tuple[str, str]:
    if a["nivel"] == "sair":
        nome = a["par"].replace("USDT", "")
        return (f"🔴 SAIR · {nome} {a['preco']:,.2f}",
                f"RSI do diário em {a['diario']['rsi']:.1f}, acima de {a['diario']['rsi_saida']}: hora de realizar o lucro.")
    nome = a["par"].replace("USDT", "")
    titulo = f"{'⭐ ENTRADA FORTE' if a['nivel'] == 'forte' else '✅ Entrada'} · {nome} {a['preco']:,.2f}"
    pl = plano(a, aj or PADRAO)
    zona = f"zona {a['h1']['minimo']}–{a['h1']['limite']}" if a['h1'].get('minimo') else f"abaixo de {a['h1']['limite']}"
    partes = [f"RSI 1h {a['h1']['rsi_fechado']:.1f} ({zona}).",
              f"Diário em {a['diario']['texto']}" + (", tendência confirmada." if a["diario"]["forca"] == "forte" else ".")]
    if a["reforcos"]:
        partes.append("A favor: " + "; ".join(a["reforcos"]) + ".")
    if pl["bons"]:
        b = pl["melhor"] or pl["bons"][0]
        rr = f" · risco/retorno {b['rr']:.1f} por 1" if b["rr"] is not None else ""
        partes.append(f"Alvo: sair em {b['preco']:,.2f} (antes da resistência {b['resistencia']:,.2f}) → lucro ~${b['liquido']:,.0f} com ${pl['valor']:,.0f}{rr}.")
    else:
        partes.append(f"Sem resistência acima que pague a taxa (precisa passar de {pl['empate']:,.2f}).")
    if pl["stop"]:
        partes.append(f"Stop: perdendo {pl['stop']['suporte']:,.2f} → ~−${abs(pl['stop']['perda']):,.0f}.")
    if a["cuidados"]:
        partes.append("Cuidado: " + "; ".join(a["cuidados"]) + ".")
    return titulo, " ".join(partes)


def texto_falado(a: dict, aj: dict) -> str:
    nome = a["par"].replace("USDT", "")
    d, h = a["diario"], a["h1"]
    if a["nivel"] == "sair":
        return (f"Senhor, hora de sair do {nome}. RSI do diário em {d['rsi']:.0f}, "
                f"acima de {d['rsi_saida']}. Pode realizar o lucro.")
    pl = plano(a, aj)
    tend = "tendência de alta" if d["lado"] == "alta" else "tendência de baixa" if d["lado"] == "baixa" else "colado na EMA 200"
    abre = "Senhor, ótima oportunidade de entrada" if a["nivel"] == "forte" else "Senhor, sinal de entrada"
    frase = (f"{abre} no {nome}. RSI do gráfico de uma hora confirmado em {h['rsi_fechado']:.0f}, "
             f"diário em {tend}" + (f" com RSI em {d['rsi']:.0f}" if d.get("rsi") is not None else "") + ". ")
    if pl["bons"]:
        b = pl["melhor"] or pl["bons"][0]
        frase += (f"Se entrar agora, dá pra sair antes da resistência do topo anterior em {numero_falado(b['resistencia'])}, "
                  f"com lucro de uns {numero_falado(b['liquido'])} dólares, já tirando a taxa")
        frase += f", {b['rr']:.0f} por 1 sobre o stop." if b["rr"] is not None else "."
    else:
        frase += f"Cuidado: não tem topo anterior perto que pague a taxa; precisa passar de {numero_falado(pl['empate'])}."
    if pl["stop"]:
        frase += f" Se perder o suporte de {numero_falado(pl['stop']['suporte'])}, a perda fica em uns {numero_falado(abs(pl['stop']['perda']))} dólares."
    return frase


# ------------------------------------------------------------------ tempos gráficos extras (só para VER na tela)
# Os sinais continuam usando só 1h e 1d (analisar). 4h, 1 semana e 1 mês são baixados quando a tela pede,
# com cache: não pesam no ciclo de 10 s.
TF_EXTRA = {"4h": {"lado": 3, "cache": 60}, "1w": {"lado": 2, "cache": 1800}, "1M": {"lado": 2, "cache": 3600}}


def niveis_grafico(cs: list[dict], lado: int, preco: float, juntar_pct: float, n: int = 3) -> dict:
    """Suportes e resistências do próprio tempo gráfico (topos/fundos já fechados), perto do preço de agora."""
    tp, fd = pivos(cs[:-1], lado)
    zs = zonas([(p, "fundo") for _, p in fd[-20:]] + [(p, "topo") for _, p in tp[-20:]], juntar_pct)
    sup = sorted([z for z in zs if z["preco"] < preco], key=lambda z: -z["preco"])[:n]
    res = sorted([z for z in zs if z["preco"] > preco], key=lambda z: z["preco"])[:n]
    for z in sup + res:
        z["dist"] = pct(z["preco"], preco)
        z["preco"] = r2(z["preco"])
    return {"suportes": sup, "resistencias": res}


# ------------------------------------------------------------------ desenhos no gráfico (chave "desenhos" do cripto.json)
# Formato (os agentes podem escrever direto no arquivo; a tela recarrega sozinha em ~1 s):
#   "desenhos": [
#     {"id": "a1b2c3", "tipo": "tendencia", "pontos": [{"t": 1793232000, "p": 2290.5}, {"t": 1793318400, "p": 2350}],
#      "cor": "#0e7490", "texto": "LTA", "autor": "agente", "estender": false}
#   ]
#   t = horário em segundos UTC (também aceita milissegundos ou "2026-10-28 14:00" em UTC); p = preço em USDT.
#   tipos e nº de pontos: horizontal 1 · tendencia 2 · raio 2 (tendência estendida para a direita) · canal 3
#   (2 da linha base + 1 que dá a largura) · funil 4 (2 da linha de cima + 2 da de baixo) · seta_cima 1 · seta_baixo 1 · texto 1.
#   autor: "usuario" ou "agente" (o do agente aparece tracejado). cor, texto e estender são opcionais.
TIPOS_DESENHO = {"horizontal": 1, "tendencia": 2, "raio": 2, "canal": 3, "funil": 4, "seta_cima": 1, "seta_baixo": 1, "texto": 1}


def _tempo_desenho(t) -> int:
    if isinstance(t, str) and not re.fullmatch(r"-?\d+(\.\d+)?", t.strip()):
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(t.strip().replace("Z", "").replace("T", " "))
        return int(dt.replace(tzinfo=timezone.utc).timestamp()) if dt.tzinfo is None else int(dt.timestamp())
    t = float(t)
    return int(t // 1000 if t > 1e11 else t)


def normalizar_desenho(d, autor_padrao: str = "agente") -> dict | None:
    """Confere e arruma um desenho. Devolve None se não der para usar."""
    try:
        tipo = str(d.get("tipo", "")).strip().lower()
        if tipo not in TIPOS_DESENHO:
            return None
        pontos = []
        for q in d.get("pontos") or []:
            pontos.append({"t": _tempo_desenho(q.get("t", 0) or 0), "p": round(float(q["p"]), 2)})
        if len(pontos) < TIPOS_DESENHO[tipo]:
            return None
        # sem id (agente escreveu à mão): id fixo tirado do conteúdo, para não mudar a cada leitura
        id_ = d.get("id") or hashlib.sha1(json.dumps([tipo, pontos, d.get("texto")], sort_keys=True).encode()).hexdigest()[:8]
        out = {"id": str(id_)[:40], "tipo": tipo,
               "pontos": pontos[:TIPOS_DESENHO[tipo]], "autor": "usuario" if d.get("autor") == "usuario" else
               ("agente" if d.get("autor") else autor_padrao)}
        cor = str(d.get("cor") or "")
        if re.fullmatch(r"#[0-9a-fA-F]{3,8}", cor):
            out["cor"] = cor
        if d.get("texto"):
            out["texto"] = str(d["texto"])[:80]
        if d.get("estender"):
            out["estender"] = True
        return out
    except Exception:
        return None


def ler_desenhos() -> list[dict] | None:
    """Desenhos do cripto.json já conferidos. None se o arquivo estiver ilegível agora (sendo gravado)."""
    try:
        bruto = json.loads(AJUSTES.read_text(encoding="utf-8")).get("desenhos") or []
    except FileNotFoundError:
        return []
    except Exception:
        return None
    out, ids = [], set()
    for d in bruto if isinstance(bruto, list) else []:
        n = normalizar_desenho(d) if isinstance(d, dict) else None
        if n:
            base, k = n["id"], 2
            while n["id"] in ids:          # id repetido: sufixo fixo (não muda entre leituras)
                n["id"] = f"{base}-{k}"; k += 1
            ids.add(n["id"]); out.append(n)
    return out


def gravar_desenhos(lista: list[dict]):
    with _TRAVA_ARQ:
        try:
            dados = json.loads(AJUSTES.read_text(encoding="utf-8"))
        except FileNotFoundError:
            dados = {}           # arquivo ilegível (ex.: alguém gravando agora): erro sobe e nada é apagado
        if not isinstance(dados, dict):
            raise ValueError("cripto.json não é um objeto")
        dados["desenhos"] = lista
        _DESENHOS_VISTOS["v"] = lista
        tmp = AJUSTES.with_suffix(".tmp")
        tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, AJUSTES)


# ------------------------------------------------------------------ avisos
def bipe():
    try:
        import winsound
        for f, d in ((880, 120), (1320, 160), (1760, 220)):
            winsound.Beep(f, d)
    except Exception as e:
        log.debug("bipe: %s", e)


ORIGEM = "pc"                      # o vigia da nuvem troca para "nuvem"
_NUVEM = {"ts": 0.0}               # último sinal de vida do vigia da nuvem (visto pelo PC)


def publicar(aj: dict, topico: str, texto: str):
    try:
        req = urllib.request.Request(f"{aj['ntfy_servidor'].rstrip('/')}/{topico}", data=texto.encode("utf-8"), method="POST")
        urllib.request.urlopen(req, timeout=8).read()
    except Exception as e:
        log.info("publicar %s: %s", topico, e)


def ler_topico(aj: dict, topico: str, desde: str) -> list[dict]:
    try:
        req = urllib.request.Request(f"{aj['ntfy_servidor'].rstrip('/')}/{topico}/json?poll=1&since={desde}",
                                     headers={"User-Agent": "Jarvis/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            linhas = r.read().decode("utf-8", "ignore").splitlines()
        return [m for m in (json.loads(l) for l in linhas if l.strip()) if m.get("event") == "message"]
    except Exception as e:
        log.info("ler %s: %s", topico, e)
        return []


def mandar_celular(aj: dict, titulo: str, msg: str, forte: bool):
    if not aj.get("celular") or not aj.get("ntfy_topico"):
        return
    if ORIGEM == "pc" and aj.get("nuvem_ativa", True) and time.time() - _NUVEM["ts"] < 2700:
        log.info("aviso pro celular fica com o vigia da nuvem: %s", titulo)
        return
    try:
        corpo = json.dumps({"topic": aj["ntfy_topico"], "title": titulo, "message": msg,
                            "priority": 5 if forte else 4,
                            "tags": ["star" if forte else "chart_with_upwards_trend"],
                            "click": f"https://www.binance.com/pt-BR/trade/{aj['par'].replace('USDT', '_USDT')}?type=spot"}).encode()
        req = urllib.request.Request(aj["ntfy_servidor"].rstrip("/"), data=corpo,
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=8).read()
        log.info("aviso enviado ao celular")
    except Exception as e:
        log.warning("celular: %s", e)


def achar_monitor(numero: int):
    import win32api
    mons = []
    for hmon, _, _ in win32api.EnumDisplayMonitors():
        info = win32api.GetMonitorInfo(hmon); mons.append(info)
        m = re.search(r"DISPLAY(\d+)", info.get("Device", ""))
        if m and int(m.group(1)) == numero:
            return info
    mons.sort(key=lambda i: (not (i["Flags"] & 1), i["Monitor"][0]))
    return mons[min(max(numero, 1), len(mons)) - 1]


def janela_hwnd():
    import win32gui
    return win32gui.FindWindow(None, TITULO)


def mostrar_no_monitor(numero: int, frente: bool = True):
    try:
        import win32con, win32gui
        alvo = achar_monitor(numero)
        h = janela_hwnd()
        if not h:
            return
        esq, topo, dir_, base = alvo["Work"]
        win32gui.ShowWindow(h, win32con.SW_RESTORE)
        win32gui.SetWindowPos(h, 0, esq + 40, topo + 40, (dir_ - esq) - 80, (base - topo) - 80, win32con.SWP_NOZORDER)
        win32gui.ShowWindow(h, win32con.SW_MAXIMIZE)
        if frente:
            # traz para a frente sem roubar o teclado de quem está digitando em outro lugar
            win32gui.SetWindowPos(h, win32con.HWND_TOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
            win32gui.SetWindowPos(h, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
            try:
                win32gui.FlashWindow(h, True)
            except Exception:
                pass
    except Exception as e:
        log.warning("mostrar: %s", e)


def minimizar():
    try:
        import win32con, win32gui
        h = janela_hwnd()
        if h:
            win32gui.ShowWindow(h, win32con.SW_MINIMIZE)
    except Exception as e:
        log.warning("minimizar: %s", e)


# ------------------------------------------------------------------ app
class Cripto:
    def __init__(self):
        self.aj = ler_ajustes()
        self.janela = None
        self.pronta = threading.Event()
        self.ultima: dict | None = None
        # o estado dos avisos fica salvo: fechar e abrir o Jarvis não repete o mesmo aviso
        est = self.aj.get("estado_avisos") or {}
        self.avisado = tuple(est["avisado"]) if est.get("avisado") else None   # (candle, nivel) já avisado
        self.armado = est.get("armado", True)       # só avisa de novo depois que o RSI sair da zona
        self.inicio = time.time()
        self.sem_historico = "estado_avisos" not in self.aj
        self.historico: list = self.aj.get("historico", [])[-30:]
        self.enviado: dict = {}      # qual pedaço do gráfico a tela já tem
        self.preco_ant = None
        self.armado_saida = est.get("armado_saida", True)
        self.ignorado = None         # (candle, nivel) de sinal que não fechou 3 por 1 (já registrado no log)
        self.cache_tf: dict = {}     # tempos gráficos extras (4h/1w/1M): {tf: (quando, candles)}
        self._trava_tf = threading.Lock()
        self._trava_des = threading.Lock()
        self._des_mtime = None       # última versão do cripto.json vista pelos desenhos
        self._des_ass = None         # desenhos que a tela já tem (para não mandar de novo)

    def js(self, fn: str, dado):
        if self.janela is not None and self.pronta.is_set():
            try:
                self.janela.evaluate_js(f"window.cr && cr.{fn}({json.dumps(dado, ensure_ascii=False)})")
            except Exception as e:
                log.debug("js: %s", e)

    def ciclo(self):
        d1 = h1 = None
        ult_d1 = 0.0
        erros = 0
        while True:
            try:
                if d1 is None or time.time() - ult_d1 > 300:
                    d1 = candles(self.aj["par"], "1d", 500); ult_d1 = time.time()
                h1 = candles(self.aj["par"], "1h", 500)
                # o preço de agora atualiza o candle de hoje do diário
                d1[-1]["c"] = h1[-1]["c"]; d1[-1]["h"] = max(d1[-1]["h"], h1[-1]["c"]); d1[-1]["l"] = min(d1[-1]["l"], h1[-1]["c"])
                a = analisar(d1, h1, self.aj)
                self.ultima = a
                erros = 0
                self.salvar_estado(a)
                self.js("atualizar", self.para_tela(a))
                self.checar_aviso(a)
                self.checar_linhas(a["preco"])
                if time.time() - self.inicio >= 50:
                    self.checar_posicao(a)
            except Exception as e:
                erros += 1
                log.warning("leitura: %s", e)
                self.js("erro", "Sem dados da Binance agora (internet/VPN?). Tentando de novo…" if erros > 1 else "")
            self.ler_comando()
            time.sleep(self.aj["intervalo"] if erros == 0 else min(60, 10 * erros))

    def para_tela(self, a: dict) -> dict:
        leve = dict(a)
        g = {}
        for tf, sr in a["grafico"].items():
            chave = (sr["t"][0], sr["t"][-1], len(sr["t"])) if sr["t"] else None
            if self.enviado.get(tf) == chave:
                g[tf] = {"parcial": True, **{k: (v[-2:] if k not in ("topos", "fundos") else v) for k, v in sr.items()}}
            else:
                g[tf] = sr
                self.enviado[tf] = chave
        leve["grafico"] = g
        leve["linhas"] = self.aj.get("linhas", [])
        leve["pos"] = self.info_posicao()
        return leve

    # -- linhas que ele desenha no gráfico: avisa quando o preço tocar
    def checar_linhas(self, preco: float):
        ant, self.preco_ant = self.preco_ant, preco
        if ant is None:
            return
        mudou = False
        for ln in self.aj.get("linhas", []):
            if not ln.get("avisar", True) or ln.get("tocou"):
                continue
            p = ln["preco"]
            if min(ant, preco) <= p <= max(ant, preco):
                ln["tocou"] = True; mudou = True
                nome = self.aj["par"].replace("USDT", "")
                titulo = f"🎯 {nome} tocou sua linha em {p:,.2f}"
                msg = f"Preço agora {preco:,.2f} ({'subindo' if preco >= ant else 'caindo'})."
                log.info("LINHA: %s", titulo)
                item = {"quando": time.strftime("%d/%m %H:%M"), "titulo": titulo, "msg": msg, "nivel": "linha", "preco": preco}
                self.historico.append(item); self.historico = self.historico[-30:]; self.aj["historico"] = self.historico
                self.js("aviso", item)
                mostrar_no_monitor(int(self.aj.get("monitor_alerta", 2)))
                if self.aj.get("bipe", True):
                    threading.Thread(target=bipe, daemon=True).start()
                threading.Thread(target=mandar_celular, args=(self.aj, titulo, msg, False), daemon=True).start()
        if mudou:
            salvar_ajustes(self.aj)
            self.publicar_estado()
            self.js("setLinhas", self.aj["linhas"])

    def add_linha(self, preco):
        ln = {"id": secrets.token_hex(4), "preco": round(float(preco), 2), "avisar": True, "tocou": False}
        self.aj.setdefault("linhas", []).append(ln); salvar_ajustes(self.aj); self.publicar_estado()
        return self.aj["linhas"]

    def remover_linha(self, id_):
        self.aj["linhas"] = [l for l in self.aj.get("linhas", []) if l["id"] != id_]; salvar_ajustes(self.aj); self.publicar_estado()
        return self.aj["linhas"]

    def alternar_linha(self, id_):
        for l in self.aj.get("linhas", []):
            if l["id"] == id_:
                if l.get("tocou"):
                    l["tocou"] = False; l["avisar"] = True     # rearma
                else:
                    l["avisar"] = not l.get("avisar", True)
        salvar_ajustes(self.aj); self.publicar_estado()
        return self.aj["linhas"]

    def mover_linha(self, id_, preco):
        """Arrastou a linha de aviso no gráfico: muda o preço e rearma o aviso."""
        try:
            preco = round(float(preco), 2)
        except Exception:
            return self.aj.get("linhas", [])
        for l in self.aj.get("linhas", []):
            if l["id"] == id_ and preco > 0:
                l["preco"] = preco; l["tocou"] = False
        salvar_ajustes(self.aj); self.publicar_estado()
        return self.aj.get("linhas", [])

    # -- tempos gráficos extras (4h, 1 semana, 1 mês): só para ver; baixa quando a tela pede, com cache
    def grafico_tf(self, tf):
        cfg = TF_EXTRA.get(tf)
        if not cfg:
            return {"erro": f"tempo gráfico desconhecido: {tf}"}
        with self._trava_tf:
            quando, cs = self.cache_tf.get(tf, (0.0, None))
            if cs is None or time.time() - quando > cfg["cache"]:
                try:
                    cs = candles(self.aj["par"], tf, 500)
                    self.cache_tf[tf] = (time.time(), cs)
                except Exception as e:
                    log.info("gráfico %s: %s", tf, e)
                    if cs is None:
                        return {"erro": "Sem dados da Binance agora."}
        cs = list(cs)
        preco = (self.ultima or {}).get("preco")
        if preco and cs and time.time() * 1000 <= cs[-1]["fim"]:
            ult = dict(cs[-1])                    # o preço de agora atualiza a vela aberta
            ult["c"] = preco; ult["h"] = max(ult["h"], preco); ult["l"] = min(ult["l"], preco)
            cs[-1] = ult
        sr = serie_grafico(cs, cfg["lado"])
        sr["niveis"] = niveis_grafico(cs, cfg["lado"], preco or cs[-1]["c"], float(self.aj.get("zona_juntar", 0.5)))
        sr["tf"], sr["cache"] = tf, cfg["cache"]
        return sr

    # -- desenhos do gráfico (ficam na chave "desenhos" do cripto.json; os agentes também escrevem lá)
    def desenhos(self):
        lista = ler_desenhos()
        if lista is None:
            return {"erro": "cripto.json ocupado, tente de novo"}
        self._des_ass = json.dumps(lista, sort_keys=True)
        return lista

    def _mudar_desenhos(self, mudar):
        with self._trava_des:
            lista = ler_desenhos()
            if lista is None:
                return {"erro": "cripto.json ocupado, tente de novo"}
            lista = mudar(lista)
            try:
                gravar_desenhos(lista)
            except Exception as e:
                log.warning("gravar desenhos: %s", e)
                return {"erro": "não consegui salvar o desenho"}
            self._des_ass = json.dumps(lista, sort_keys=True)
            return lista

    def salvar_desenho(self, d):
        n = normalizar_desenho(d if isinstance(d, dict) else {}, autor_padrao="usuario")
        if not n:
            return {"erro": "desenho inválido"}
        def trocar(ls):           # mesmo id = atualiza no lugar; id novo = acrescenta no fim
            if any(x["id"] == n["id"] for x in ls):
                return [n if x["id"] == n["id"] else x for x in ls]
            return ls + [n]
        return self._mudar_desenhos(trocar)

    def apagar_desenho(self, id_):
        return self._mudar_desenhos(lambda ls: [x for x in ls if x["id"] != id_])

    def limpar_desenhos(self, autor=None):
        return self._mudar_desenhos(lambda ls: [x for x in ls if autor and x.get("autor") != autor])

    def checar_desenhos(self):
        """Se o cripto.json mudou (ex.: um agente marcou algo), manda os desenhos novos para a tela."""
        try:
            m = AJUSTES.stat().st_mtime_ns
        except Exception:
            return
        if m == self._des_mtime:
            return
        lista = ler_desenhos()
        if lista is None:
            return                     # sendo gravado: olha de novo na próxima volta
        self._des_mtime = m
        ass = json.dumps(lista, sort_keys=True)
        if ass != self._des_ass:
            self._des_ass = ass
            self.js("setDesenhos", lista)

    def _salvar_estado_avisos(self):
        novo = {"avisado": list(self.avisado) if self.avisado else None, "armado": self.armado, "armado_saida": self.armado_saida}
        if self.aj.get("estado_avisos") != novo:
            self.aj["estado_avisos"] = novo
            salvar_ajustes(self.aj)

    def checar_aviso(self, a: dict):
        if time.time() - self.inicio < 50:
            return          # deixa a abertura do Jarvis (música + saudação) terminar; o resumo do dia já fala do mercado
        if self.sem_historico:                    # 1ª vez com esta versão: só registra a situação atual, sem avisar
            self.sem_historico = False
            if a["nivel"] in ("entrada", "forte"):
                self.avisado, self.armado = (a["candle_1h"], a["nivel"]), False
            if a["nivel"] == "sair":
                self.armado_saida = False
            self._salvar_estado_avisos()
            return
        try:
            self._checar_aviso(a)
        finally:
            self._salvar_estado_avisos()

    def _checar_aviso(self, a: dict):
        if self.aj.get("posicao"):
            return                     # já está dentro de uma operação: os avisos são os da operação
        rd = a["diario"].get("rsi")
        if a["nivel"] == "sair":
            if self.armado_saida:
                self.armado_saida = False
                self.disparar(a)
            return
        if rd is not None and rd < a["diario"]["rsi_saida"] - 3:
            self.armado_saida = True     # o diário esfriou: a próxima saída avisa de novo
        if a["nivel"] not in ("entrada", "forte"):
            if a["h1"]["rsi_fechado"] is not None and a["h1"]["rsi_fechado"] >= a["h1"]["limite"]:
                self.armado = True       # saiu da zona: o próximo sinal avisa de novo
            return
        chave = (a["candle_1h"], a["nivel"])
        # entrada que vira "forte" no mesmo sinal NÃO é aviso novo (era o que fazia ele falar de novo do nada)
        if not self.armado:
            return
        if self.avisado == chave:
            return
        pl = plano(a, self.aj)
        if not pl["melhor"]:
            if self.ignorado != chave:         # registra uma vez por candle; se o risco/retorno melhorar, avisa ainda neste sinal
                self.ignorado = chave
                b = pl["bons"][0] if pl["bons"] else None
                log.info("sinal ignorado (%s %.2f): sem alvo de %.0f por 1 — melhor lucro %s, stop %s", a["nivel"], a["preco"],
                         pl["rr_min"], f"${b['liquido']:,.0f}" if b else "nenhum",
                         f"−${abs(pl['stop']['perda']):,.0f}" if pl["stop"] else "sem suporte")
            return
        self.avisado, self.armado = chave, False
        self.disparar(a)

    def disparar(self, a: dict):
        titulo, msg = texto_aviso(a, self.aj)
        log.info("AVISO: %s — %s", titulo, msg)
        item = {"quando": time.strftime("%d/%m %H:%M"), "titulo": titulo, "msg": msg, "nivel": a["nivel"], "preco": a["preco"]}
        self.historico.append(item)
        self.historico = self.historico[-30:]
        self.aj["historico"] = self.historico
        salvar_ajustes(self.aj)
        self.js("aviso", item)
        mostrar_no_monitor(int(self.aj.get("monitor_alerta", 2)))
        if self.aj.get("bipe", True):
            threading.Thread(target=bipe, daemon=True).start()
        threading.Thread(target=mandar_celular, args=(self.aj, titulo, msg, a["nivel"] in ("forte", "sair")), daemon=True).start()
        if self.aj.get("voz", True):
            try:
                FALA.write_text(texto_falado(a, self.aj), encoding="utf-8")     # o Jarvis lê e fala
            except Exception as e:
                log.warning("fala: %s", e)

    # ------------------------------------------------------------ sincronização PC <-> nuvem (tópico ntfy "-sync")
    def publicar_estado(self, operacao=None, apagar=None):
        if not self.aj.get("ntfy_topico"):
            return
        self.sync_ts = time.time()
        corpo = {"tipo": "estado", "origem": ORIGEM, "ts": self.sync_ts, "posicao": self.aj.get("posicao"),
                 "linhas": self.aj.get("linhas", []), "valor_trade": self.aj.get("valor_trade"),
                 "operacao": operacao, "apagar": apagar}
        threading.Thread(target=publicar, args=(self.aj, self.aj["ntfy_topico"] + "-sync", json.dumps(corpo, ensure_ascii=False)),
                         daemon=True).start()

    def aplicar_estado(self, m: dict):
        if m.get("ts", 0) <= getattr(self, "sync_ts", 0):
            return
        self.sync_ts = m["ts"]
        self.aj["posicao"] = m.get("posicao")
        self.aj["linhas"] = m.get("linhas") or []
        if m.get("valor_trade"):
            self.aj["valor_trade"] = m["valor_trade"]
        ops = self.aj.setdefault("operacoes", [])
        op = m.get("operacao")
        if op and not any(o["fim"] == op["fim"] and abs(o["entrada"] - op["entrada"]) < 0.01 for o in ops):
            ops.append(op)
        ap = m.get("apagar")
        if ap:
            self.aj["operacoes"] = [o for o in ops if not (o["fim"] == ap["fim"] and abs(o["entrada"] - ap["entrada"]) < 0.01)]
        salvar_ajustes(self.aj)
        log.info("sincronizado (%s): posição %s", m.get("origem"), "aberta" if self.aj.get("posicao") else "nenhuma")
        self.js("setPosicao", self.info_posicao())
        self.js("setLinhas", self.aj.get("linhas", []))

    def sincronizar(self):
        """Olha o tópico -sync a cada 30 s: estado vindo do outro lado (PC/nuvem) e sinal de vida da nuvem."""
        desde, ult_pub, ult_vivo = "12h", 0.0, 0.0
        self.sync_ts = getattr(self, "sync_ts", 0.0)
        while True:
            try:
                msgs = ler_topico(self.aj, self.aj["ntfy_topico"] + "-sync", desde)
                for ev in msgs:
                    desde = ev.get("id", desde)
                    try:
                        m = json.loads(ev.get("message", ""))
                    except Exception:
                        continue
                    if m.get("origem") == ORIGEM:
                        continue
                    if m.get("tipo") == "vivo":
                        _NUVEM["ts"] = max(_NUVEM["ts"], float(m.get("ts", 0)))
                    elif m.get("tipo") == "estado":
                        self.aplicar_estado(m)
                if ORIGEM == "nuvem" and time.time() - ult_vivo > 600:
                    ult_vivo = time.time()
                    publicar(self.aj, self.aj["ntfy_topico"] + "-sync",
                             json.dumps({"tipo": "vivo", "origem": ORIGEM, "ts": time.time()}))
                if time.time() - ult_pub > 6 * 3600 and (self.aj.get("posicao") or self.aj.get("linhas")):
                    ult_pub = time.time()                 # o ntfy guarda só 12 h: republica de tempos em tempos
                    self.publicar_estado()
            except Exception as e:
                log.info("sincronizar: %s", e)
            time.sleep(30)

    # ------------------------------------------------------------ operação em andamento (entrei / saí)
    def avisar(self, titulo: str, msg: str, nivel: str, fala: str, forte=True):
        log.info("AVISO: %s — %s", titulo, msg)
        item = {"quando": time.strftime("%d/%m %H:%M"), "titulo": titulo, "msg": msg, "nivel": nivel, "preco": self.preco_ant or 0}
        self.historico.append(item); self.historico = self.historico[-30:]; self.aj["historico"] = self.historico
        salvar_ajustes(self.aj)
        self.js("aviso", item)
        mostrar_no_monitor(int(self.aj.get("monitor_alerta", 2)))
        if self.aj.get("bipe", True):
            threading.Thread(target=bipe, daemon=True).start()
        threading.Thread(target=mandar_celular, args=(self.aj, titulo, msg, forte), daemon=True).start()
        if self.aj.get("voz", True) and fala:
            try:
                FALA.write_text(fala, encoding="utf-8")
            except Exception as e:
                log.warning("fala: %s", e)

    def _alvo_stop_auto(self, preco):
        a = self.ultima or {}
        if not a:
            return None, None
        pl = plano(a, self.aj)
        b = pl.get("melhor") or (pl["bons"][0] if pl.get("bons") else None)
        alvo = b["preco"] if b else None
        stop = pl["stop"]["preco"] if pl.get("stop") else None
        return alvo, stop

    def entrar(self, preco=None, valor=None, alvo=None, stop=None):
        try:
            preco = float(str(preco).replace(",", "")) if preco not in (None, "") else float(self.preco_ant or 0)
            valor = float(str(valor).replace(",", "")) if valor not in (None, "") else float(self.aj["valor_trade"])
        except Exception:
            return {"erro": "preço ou valor inválido"}
        if preco <= 0:
            return {"erro": "ainda sem preço da Binance"}
        a_auto, s_auto = self._alvo_stop_auto(preco)
        self.aj["posicao"] = {"preco": preco, "valor": valor, "hora": time.strftime("%Y-%m-%d %H:%M"), "ts": time.time(),
                              "alvo": float(alvo) if alvo else a_auto, "stop": float(stop) if stop else s_auto,
                              "avisos": {"alvo": False, "stop": False, "saida": False, "empate": False}}
        salvar_ajustes(self.aj)
        log.info("ENTROU: %s", self.aj["posicao"])
        self.publicar_estado()
        self.js("setPosicao", self.info_posicao())
        return self.info_posicao()

    def sair(self, preco=None):
        pos = self.aj.get("posicao")
        if not pos:
            return {"erro": "não tem operação aberta"}
        try:
            preco = float(str(preco).replace(",", "")) if preco not in (None, "") else float(self.preco_ant or pos["preco"])
        except Exception:
            preco = float(self.preco_ant or pos["preco"])
        taxa = pos["valor"] * float(self.aj["taxa_pct"]) / 100
        lucro = pos["valor"] * (preco - pos["preco"]) / pos["preco"] - taxa
        op = {"entrada": pos["preco"], "saida": preco, "valor": pos["valor"], "lucro": round(lucro, 2),
              "inicio": pos["hora"], "fim": time.strftime("%Y-%m-%d %H:%M"),
              "horas": round((time.time() - pos.get("ts", time.time())) / 3600, 1)}
        self.aj.setdefault("operacoes", []).append(op)
        self.aj["operacoes"] = self.aj["operacoes"][-200:]
        self.aj["posicao"] = None
        # volta a esperar entrada — mas um sinal NOVO: se o RSI ainda estiver na zona, só avisa depois que sair e voltar
        self.armado = False
        if self.ultima:
            self.avisado = (self.ultima.get("candle_1h"), self.ultima.get("nivel"))
        salvar_ajustes(self.aj)
        log.info("SAIU: %s", op)
        self.publicar_estado(operacao=op)
        self.js("setPosicao", self.info_posicao())
        return {"ok": True, "operacao": op, **self.info_posicao()}

    def apagar_operacao(self, fim, entrada):
        ops = self.aj.get("operacoes", [])
        self.aj["operacoes"] = [o for o in ops if not (o["fim"] == fim and abs(o["entrada"] - float(entrada)) < 0.01)]
        salvar_ajustes(self.aj)
        self.publicar_estado(apagar={"fim": fim, "entrada": float(entrada)})
        return self.info_posicao()

    def set_alvo_stop(self, alvo=None, stop=None):
        pos = self.aj.get("posicao")
        if not pos:
            return self.info_posicao()
        for k, v in (("alvo", alvo), ("stop", stop)):
            if v not in (None, ""):
                try:
                    pos[k] = float(str(v).replace(",", "")); pos["avisos"][k] = False
                except Exception:
                    pass
        salvar_ajustes(self.aj)
        self.publicar_estado()
        return self.info_posicao()

    def info_posicao(self):
        pos = self.aj.get("posicao")
        ops = self.aj.get("operacoes", [])
        mes = time.strftime("%Y-%m")
        do_mes = [o for o in ops if o["fim"].startswith(mes)]
        res = {"posicao": None, "operacoes": ops[-10:][::-1], "mes_total": round(sum(o["lucro"] for o in do_mes), 2),
               "mes_qtd": len(do_mes), "taxa_pct": self.aj["taxa_pct"]}
        if pos:
            p = float(self.preco_ant or pos["preco"])
            taxa = pos["valor"] * float(self.aj["taxa_pct"]) / 100
            bruto = pos["valor"] * (p - pos["preco"]) / pos["preco"]
            res["posicao"] = {**pos, "atual": p, "lucro": round(bruto - taxa, 2), "pct": round((p - pos["preco"]) / pos["preco"] * 100, 2),
                              "empate": pos["preco"] * (1 + float(self.aj["taxa_pct"]) / 100)}
        return res

    def checar_posicao(self, a: dict):
        pos = self.aj.get("posicao")
        if not pos:
            return
        info = self.info_posicao()["posicao"]
        av, p, nome = pos["avisos"], a["preco"], a["par"].replace("USDT", "")
        fmt = lambda v: f"{v:,.0f}".replace(",", ".")
        lucro = info["lucro"]
        txt_l = f"lucro de uns {fmt(lucro)} dólares" if lucro >= 0 else f"prejuízo de uns {fmt(-lucro)} dólares"
        sinal = "+" if lucro >= 0 else "−"
        rd = a["diario"].get("rsi")
        mudou = False
        if not av["saida"] and rd is not None and rd > a["diario"]["rsi_saida"]:
            av["saida"] = mudou = True
            self.avisar(f"🔴 SAIR · {nome} {p:,.2f} · {sinal}${abs(lucro):,.0f}",
                        f"RSI do diário em {rd:.1f}, acima de {a['diario']['rsi_saida']}: pela sua regra, hora de realizar. Resultado agora: {lucro:+,.0f} dólares.",
                        "sair", f"Chefe, hora de sair do {nome}. O RSI do diário passou de {a['diario']['rsi_saida']}. Saindo agora, {txt_l}, já com a taxa.")
        if not av["alvo"] and pos.get("alvo") and p >= pos["alvo"]:
            av["alvo"] = mudou = True
            self.avisar(f"🎯 ALVO · {nome} {p:,.2f} · {sinal}${abs(lucro):,.0f}",
                        f"Bateu o alvo de {pos['alvo']:,.2f}. Resultado agora: {lucro:+,.0f} dólares (com taxa).",
                        "forte", f"Chefe, o {nome} bateu o seu alvo de {fmt(pos['alvo'])}. Dá {txt_l}, já tirando a taxa.")
        if not av["stop"] and pos.get("stop") and p <= pos["stop"]:
            av["stop"] = mudou = True
            self.avisar(f"🛑 STOP · {nome} {p:,.2f} · {sinal}${abs(lucro):,.0f}",
                        f"Perdeu o stop de {pos['stop']:,.2f}. Resultado agora: {lucro:+,.0f} dólares (com taxa).",
                        "sair", f"Atenção, chefe: o {nome} perdeu o seu stop em {fmt(pos['stop'])}. Agora é {txt_l}.")
        if mudou:
            av["empate"] = av["empate"] or lucro > 0     # já avisou algo maior nesta rodada
        if not av["empate"] and lucro > 0:
            av["empate"] = mudou = True
            self.avisar(f"💰 Pagou a taxa · {nome} {p:,.2f}", "A operação já cobriu a taxa de entrada e saída; daqui pra cima é lucro.",
                        "linha", f"Chefe, o trade do {nome} já pagou a taxa. Daqui pra cima é lucro.", forte=False)
        if mudou:
            salvar_ajustes(self.aj)
            self.publicar_estado()

    def set_valor(self, v):
        try:
            v = float(str(v).replace(",", ""))
            if v > 0:
                self.aj["valor_trade"] = v; salvar_ajustes(self.aj)
        except Exception:
            pass
        return self.aj["valor_trade"]

    def salvar_estado(self, a: dict):
        try:
            leve = {k: v for k, v in a.items() if k != "grafico"}
            leve["plano"] = plano(a, self.aj)
            tmp = ESTADO.with_suffix(".tmp")
            tmp.write_text(json.dumps(leve, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, ESTADO)
        except Exception as e:
            log.debug("estado: %s", e)

    def ler_comando(self):
        try:
            if CMD.exists():
                bruto = CMD.read_text(encoding="utf-8").strip()
                CMD.unlink()
                c = bruto.lower()
                if c.startswith("{"):
                    pedido = json.loads(bruto)
                    if pedido.get("acao") == "entrar":
                        r = self.entrar(pedido.get("preco"), pedido.get("valor"), pedido.get("alvo"), pedido.get("stop"))
                    elif pedido.get("acao") == "sair":
                        r = self.sair(pedido.get("preco"))
                    elif pedido.get("acao") == "ajustar":
                        r = self.set_alvo_stop(pedido.get("alvo"), pedido.get("stop"))
                    else:
                        r = {"erro": "ação desconhecida"}
                    (PASTA / "cripto_resposta.json").write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
                    return
                if c == "mostrar":
                    mostrar_no_monitor(int(self.aj.get("monitor_alerta", 2)))
                elif c == "minimizar":
                    minimizar()
                elif c == "testar":
                    self.testar_aviso()
                elif c == "sair":
                    os._exit(0)
        except Exception as e:
            log.debug("cmd: %s", e)

    def testar_aviso(self):
        item = {"quando": time.strftime("%d/%m %H:%M"), "titulo": "🔔 Teste do Modo Cripto",
                "msg": "Se você recebeu isto no celular, os avisos estão funcionando.", "nivel": "teste", "preco": 0}
        self.js("aviso", item)
        mostrar_no_monitor(int(self.aj.get("monitor_alerta", 2)))
        threading.Thread(target=bipe, daemon=True).start()
        threading.Thread(target=mandar_celular, args=(dict(self.aj, celular=True), item["titulo"], item["msg"], False), daemon=True).start()
        return True

    def info_inicial(self):
        return {"valor": self.aj["valor_trade"], "taxa_pct": self.aj["taxa_pct"], "margem": self.aj["margem_alvo"],
                "rr_minimo": float(self.aj.get("rr_minimo", 3)),
                "linhas": self.aj.get("linhas", []), "topico": self.aj["ntfy_topico"], "servidor": self.aj["ntfy_servidor"], "historico": self.historico,
                "celular": self.aj.get("celular", True), "monitor": self.aj.get("monitor_alerta", 2),
                "desenhos": (lambda d: d if isinstance(d, list) else [])(self.desenhos())}

    def set_celular(self, ligado):
        self.aj["celular"] = bool(ligado); salvar_ajustes(self.aj)
        return self.aj["celular"]


class ApiPagina:
    """Só o que a página pode chamar."""
    def __init__(self, app):
        self._app = app

    def testar_aviso(self):
        return self._app.testar_aviso()

    def set_celular(self, ligado):
        return self._app.set_celular(ligado)

    def minimizar(self):
        minimizar(); return True

    def add_linha(self, preco):
        return self._app.add_linha(preco)

    def remover_linha(self, id_):
        return self._app.remover_linha(id_)

    def alternar_linha(self, id_):
        return self._app.alternar_linha(id_)

    def log_js(self, msg):
        log.warning("tela: %s", str(msg)[:500]); return True

    def set_valor(self, v):
        return self._app.set_valor(v)

    def entrar(self, preco=None, valor=None):
        return self._app.entrar(preco, valor)

    def sair(self, preco=None):
        return self._app.sair(preco)

    def set_alvo_stop(self, alvo=None, stop=None):
        return self._app.set_alvo_stop(alvo, stop)

    def apagar_operacao(self, fim, entrada):
        return self._app.apagar_operacao(fim, entrada)

    def recarregar(self):
        self._app.enviado = {}; self._app._des_mtime = self._app._des_ass = None; return True

    def mover_linha(self, id_, preco):
        return self._app.mover_linha(id_, preco)

    def grafico(self, tf):
        return self._app.grafico_tf(tf)

    def desenhos(self):
        return self._app.desenhos()

    def salvar_desenho(self, d):
        return self._app.salvar_desenho(d)

    def apagar_desenho(self, id_):
        return self._app.apagar_desenho(id_)

    def limpar_desenhos(self, autor=None):
        return self._app.limpar_desenhos(autor)


LIB = PASTA / "lightweight-charts.js"
LIB_URLS = ("https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js",
            "https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js")


def garantir_biblioteca():
    if LIB.exists() and LIB.stat().st_size > 50000:
        return
    for url in LIB_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Jarvis/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                dados = r.read()
            if len(dados) > 50000 and b"LightweightCharts" in dados:
                LIB.write_bytes(dados); log.info("biblioteca do gráfico salva (%d KB)", len(dados) // 1024)
                return
        except Exception as e:
            log.warning("biblioteca do gráfico (%s): %s", url, e)


def main():
    garantir_biblioteca()
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    import webview
    app = Cripto()
    janela = webview.create_window(TITULO, str(PASTA / "cripto.html"), width=1300, height=820,
                                   background_color="#070b10", js_api=ApiPagina(app), minimized=True)
    app.janela = janela

    def carregou():
        app.pronta.set()
        app.enviado = {}
        app.js("iniciar", app.info_inicial())
        if app.ultima:
            app.js("atualizar", app.para_tela(app.ultima))
        def posicionar():
            # deixa a janela já no monitor 2 (minimizada) para abrir lá quando avisar
            time.sleep(1.5)
            try:
                import win32con, win32gui
                h = janela_hwnd()
                if h:
                    alvo = achar_monitor(int(app.aj.get("monitor_alerta", 2)))
                    esq, topo, dir_, base = alvo["Work"]
                    wp = list(win32gui.GetWindowPlacement(h))
                    wp[1] = win32con.SW_SHOWMINNOACTIVE
                    wp[4] = (esq + 40, topo + 40, dir_ - 40, base - 40)
                    win32gui.SetWindowPlacement(h, tuple(wp))
            except Exception as e:
                log.debug("posicionar: %s", e)
        threading.Thread(target=posicionar, daemon=True).start()

    janela.events.loaded += carregou
    janela.events.closed += lambda: os._exit(0)
    log.info("Modo Cripto iniciado (%s), avisos no monitor %s, celular: %s", app.aj["par"], app.aj["monitor_alerta"], app.aj["ntfy_topico"])
    def comandos():
        while True:
            app.ler_comando()
            app.checar_desenhos()          # um agente marcou algo no cripto.json: aparece na tela
            time.sleep(1)
    threading.Thread(target=comandos, daemon=True).start()
    threading.Thread(target=app.sincronizar, daemon=True, name="sync").start()
    webview.start(lambda: threading.Thread(target=app.ciclo, daemon=True).start())
    os._exit(0)


if __name__ == "__main__":
    main()
