"""Vigia do ETH no GitHub Actions: roda 1 vez a cada ~10 min (PC pode estar desligado).
Usa as mesmas regras do Modo Cripto (cripto.py) e guarda o estado entre as rodadas na pasta estado/ (cache do Actions).
O canal do ntfy vem do segredo NTFY_TOPICO (não fica público no repositório). Nunca compra nem vende nada.
"""
import json
import os
import time
from pathlib import Path

import cripto

EST = Path(__file__).resolve().parent / "estado"
EST.mkdir(exist_ok=True)
cripto.AJUSTES = EST / "cripto.json"
cripto.ESTADO = EST / "cripto_estado.json"
cripto.FALA = EST / "fala.txt"
cripto.CMD = EST / "cmd.txt"
cripto.ORIGEM = "nuvem"
cripto.mostrar_no_monitor = lambda *a, **k: None
cripto.minimizar = lambda *a, **k: None
cripto.bipe = lambda: None
log = cripto.log

import vigia_nuvem   # reaproveita os comandos do celular (entrei / sai / stop / alvo / status)


def main():
    topico = os.environ.get("NTFY_TOPICO", "").strip()
    if not topico:
        raise SystemExit("falta o segredo NTFY_TOPICO")
    if not cripto.AJUSTES.exists():
        cripto.AJUSTES.write_text(json.dumps({"ntfy_topico": topico}), encoding="utf-8")
    app = vigia_nuvem.Nuvem()
    app.aj["ntfy_topico"] = topico
    app.aj["celular"] = True
    app.inicio = time.time() - 120
    app.preco_ant = app.aj.get("_preco_ant")
    app.sync_ts = app.aj.get("_sync_ts", 0.0)

    # 1) o que mudou no PC (posição, linhas, valor)
    desde = app.aj.get("_sync_id") or "12h"
    for ev in cripto.ler_topico(app.aj, topico + "-sync", desde):
        desde = ev.get("id", desde)
        try:
            m = json.loads(ev.get("message", ""))
        except Exception:
            continue
        if m.get("origem") != "nuvem" and m.get("tipo") == "estado":
            app.aplicar_estado(m)
    app.aj["_sync_id"] = desde

    # 2) mercado + avisos (as mesmas regras do PC)
    d1 = cripto.candles(app.aj["par"], "1d", 500)
    h1 = cripto.candles(app.aj["par"], "1h", 500)
    d1[-1]["c"] = h1[-1]["c"]; d1[-1]["h"] = max(d1[-1]["h"], h1[-1]["c"]); d1[-1]["l"] = min(d1[-1]["l"], h1[-1]["c"])
    a = cripto.analisar(d1, h1, app.aj)
    app.ultima = a
    app.checar_aviso(a)
    app.checar_linhas(a["preco"])
    app.checar_posicao(a)
    log.info("ETH %.2f · sinal %s · posição %s", a["preco"], a["nivel"], "aberta" if app.aj.get("posicao") else "nenhuma")

    # 3) comandos mandados pelo celular (canal -cmd)
    cdesde = app.aj.get("_cmd_id") or "15m"
    for ev in cripto.ler_topico(app.aj, topico + "-cmd", cdesde):
        cdesde = ev.get("id", cdesde)
        app.tratar_comando((ev.get("message") or "").strip())
    app.aj["_cmd_id"] = cdesde

    # 4) sinal de vida (o PC não manda pro celular enquanto a nuvem estiver viva) e guarda o estado
    cripto.publicar(app.aj, topico + "-sync", json.dumps({"tipo": "vivo", "origem": "nuvem", "ts": time.time()}))
    app.aj["_preco_ant"] = a["preco"]
    app.aj["_sync_ts"] = getattr(app, "sync_ts", 0.0)
    app.aj.pop("historico", None)
    cripto.salvar_ajustes(app.aj)
    time.sleep(2)                       # deixa os avisos em segundo plano terminarem de enviar


if __name__ == "__main__":
    main()
